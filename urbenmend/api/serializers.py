"""
camelCase field naming (A8, T0.6).

API §1.2 requires `camelCase` JSON bodies; DRF serializers emit `snake_case` from model field
names. The docs name this gap explicitly as "the single easiest way for the implementation to
silently drift", so the rename is a first-class layer rather than per-serializer `source=`
plumbing that one forgotten field would break.

**Why a serializer mixin and not a global renderer.** A renderer-level rename rewrites every key
in the response, including keys that must stay verbatim:

  - `error.details[].field` names the client's *own submitted* field (API §4.2), and only the
    serializer knows which keys are its fields and which are free-form content. It renames its own
    error keys (see `run_validation`); a renderer rewriting the whole envelope would also rename
    keys inside a `message`, a rationale blob, or a carried payload.
  - GeoJSON's `type` / `geometry` / `coordinates` / `properties` are fixed by RFC 7946 and by
    §4.3's `FeatureCollection` payloads. A renderer cannot tell them apart from domain fields.

Applying the rename at the serializer boundary keeps it to fields this project defines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from rest_framework import serializers
from rest_framework.fields import empty

# Matches `_x` where x is alphanumeric, so `snake_case` → `snakeCase`. A trailing underscore or
# a double underscore is left alone rather than silently collapsed — those are typos worth
# surfacing, not input to normalize.
_SNAKE_SEGMENT = re.compile(r"_([a-z0-9])")


def to_camel_case(value: str) -> str:
    """`report_id` → `reportId`. Idempotent: an already-camelCase name is returned unchanged."""
    return _SNAKE_SEGMENT.sub(lambda match: match.group(1).upper(), value)


def to_snake_case(value: str) -> str:
    """`reportId` → `report_id`. The inverse for inbound request bodies."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def camelize_error_detail(detail: Any) -> Any:
    """Rename the field keys inside a DRF `ValidationError.detail`, at any depth.

    ⚠️ **Leaves are returned untouched, and that is load-bearing.** DRF's `ErrorDetail` is a `str`
    subclass carrying `.code`, which `_flatten_validation_detail` reads as the contract's `issue`
    (`REQUIRED`, `INVALID`). Rebuilding a leaf as a plain `str` would drop that code and every
    `issue` would silently fall back to `INVALID`. Only dict *keys* are rewritten.

    ⚠️ **Recursion is safe because `detail` is only ever dicts, lists and string leaves** — never a
    dict used as a message payload — so no free-form content can have its keys rewritten. A nested
    `CamelCaseSerializer` has already renamed its own keys by the time the parent nests them, and
    `to_camel_case` is idempotent, so the second pass is a no-op rather than a corruption.
    """
    if isinstance(detail, dict):
        return {
            to_camel_case(str(key)): camelize_error_detail(value) for key, value in detail.items()
        }
    if isinstance(detail, list):
        return [camelize_error_detail(item) for item in detail]
    return detail


class CamelCaseSerializerMixin:
    """Rename declared fields to camelCase on the way out and back on the way in.

    Mix in **before** the serializer base class so its `fields` property resolves through here:

        class ReportSerializer(CamelCaseSerializerMixin, serializers.ModelSerializer): ...

    ⚠️ Renames only the serializer's own declared fields. Nested `SerializerMethodField` return
    values, free-form `JSONField` contents, and GeoJSON structural keys pass through untouched —
    which is the intent, since those are not this project's field names to rewrite.
    """

    def to_representation(self, instance: Any) -> dict[str, Any]:
        representation = super().to_representation(instance)  # type: ignore[misc]
        return {to_camel_case(key): value for key, value in representation.items()}

    def to_internal_value(self, data: Any) -> Any:
        # ⚠️ Guarded: `data` is client-supplied and DRF only guarantees a Mapping for object
        # bodies. A list or scalar body must reach super() unchanged so it raises DRF's own
        # validation error rather than an AttributeError here (which would surface as a 500).
        if isinstance(data, dict):
            data = {to_snake_case(key): value for key, value in data.items()}
        return super().to_internal_value(data)  # type: ignore[misc]

    def run_validation(self, data: Any = empty) -> Any:
        """Rename rejected field names back to camelCase before the error leaves the serializer.

        ⚠️ **The rename has to happen on the error too, not just on the payload.** DRF raises with
        the serializer's *own* field names, so a rejected `mediaIds` is reported as `media_ids` —
        a field the client never sent. `error.details[].field` names the client's own submitted
        field (API §4.2, and the reasoning in this module's docstring), so the inbound rename in
        `to_internal_value` is only half of the boundary.

        ⚠️ **Wrapped here, not in `to_internal_value`.** This is the single funnel: DRF runs
        `to_internal_value` (field checks and `validate_<field>`) *and* `validate()` from inside
        `run_validation`, so an object-level rejection is covered by the same three lines. Wrapping
        the narrower method would leave `validate()`'s errors in `snake_case` — the exact silent
        half-fix this layer exists to prevent.
        """
        try:
            return super().run_validation(data)  # type: ignore[misc]
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(camelize_error_detail(exc.detail)) from exc


class CamelCaseSerializer(CamelCaseSerializerMixin, serializers.Serializer[Any]):
    """Plain (non-model) serializer with the rename applied."""


class CamelCaseModelSerializer(CamelCaseSerializerMixin, serializers.ModelSerializer[Any]):
    """Model serializer with the rename applied. The default base for API resources."""


def reject_unknown_fields(
    # ⚠️ `Serializer`, not `BaseSerializer`: `fields` is declared on the former. The wider annotation
    # reads as more accommodating and is simply wrong — a `ListSerializer` or a bare `BaseSerializer`
    # has no `fields`, so accepting one here would be an `AttributeError` at runtime.
    serializer: serializers.Serializer[Any],
    *,
    extra_allowed: Iterable[str] = (),
    message: str = "This field is not accepted by this endpoint.",
) -> None:
    """Refuse keys the endpoint does not declare, instead of silently dropping them.

    Call from a serializer's `validate()`. Raises DRF's `ValidationError` — a `400` with one
    `details[]` entry per unknown key (API §4.1).

    ⚠️ **DRF's default is to discard unknown keys, and the failure that causes is a silent success.**
    `POST /reports {"severity": "critical"}` answers `202` and the citizen believes they filed a
    Critical report; `PATCH /users/me {"role": "admin"}` answers `200`. Every field adjacent to these
    bodies is derived data that api-conventions.md makes read-only to all clients, so "ignored" and
    "accepted" are indistinguishable to the caller.

    ⚠️ **A function, not a mixin.** Three serializers need this rule and a fourth (T1.9's
    `ProfileUpdateSerializer`) already carries its own copy — but a mixin would have to sit at a
    fixed place in the MRO relative to `CamelCaseSerializerMixin` to see the renamed keys, and
    getting that order wrong fails open rather than loudly. One call in one `validate()` cannot be
    mis-ordered.

    ⚠️ **Both spellings are allowed, and that is not belt-and-braces.**
    `CamelCaseSerializerMixin.to_internal_value()` has already rewritten the keys by the time
    `validate()` runs, while `initial_data` keeps the client's originals — so comparing against
    `serializer.fields` alone would flag the caller's own `mediaIds` as an unknown field.

    ⚠️ **`extra_allowed` exists for keys that are real but belong to another layer** — `?limit=` and
    `?cursor=` are the paginator's, not the serializer's, and a query serializer that refused them
    would make every second page a `400`.

    The trade-off, stated rather than hidden: a client written against a *newer* additive field gets
    a `400` from this server instead of having it ignored. Additive fields must be declared as they
    ship, which is required for them to work at all.
    """
    # ⚠️ `.keys()`, not iteration: DRF's `BindingDict` yields names at runtime but its stubs type
    # `__iter__` as yielding `Field`, so plain iteration type-checks and then compares the wrong
    # objects.
    declared = {str(name) for name in serializer.fields.keys()}  # noqa: SIM118
    allowed = declared | {to_camel_case(name) for name in declared} | set(extra_allowed)
    submitted = set(serializer.initial_data) if isinstance(serializer.initial_data, dict) else set()

    if unknown := sorted(submitted - allowed):
        raise serializers.ValidationError(dict.fromkeys(unknown, message), code="VALIDATION_FAILED")


def allowlisted_csv(value: str, *, allowed: set[str], label: str) -> list[str]:
    """`?severity=high,medium` → `["high", "medium"]`, refusing anything off the allowlist.

    §4.4 fixes comma separation for repeated filter values, so the split belongs in one place rather
    than in each query serializer.

    ⚠️ **An invalid *value* is a `400`, never an empty page.** `?status=bogus` matches nothing, and
    "no results" is indistinguishable from "you asked the wrong question" — a citizen filtering their
    own open reports would be shown an empty list and conclude they had none. Every enum this guards
    is public (§6.13's `/meta/enums`, the taxonomy), so naming the offending value leaks nothing.

    ⚠️ **`code="INVALID"`, not the default.** `_flatten_validation_detail` reads the code as the
    contract's `details[].issue`, so without it a rejected value reports the same `issue` as a missing
    one and a client cannot tell "you omitted this" from "that word is not a status".
    """
    submitted = [item.strip() for item in value.split(",") if item.strip()]
    if not submitted:
        raise serializers.ValidationError(f"Provide at least one {label} value.")
    if unknown := sorted(set(submitted) - allowed):
        raise serializers.ValidationError(
            f"Unknown {label} value(s): {', '.join(unknown)}.", code="INVALID"
        )
    return submitted
