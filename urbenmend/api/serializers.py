"""
camelCase field naming (A8, T0.6).

API §1.2 requires `camelCase` JSON bodies; DRF serializers emit `snake_case` from model field
names. The docs name this gap explicitly as "the single easiest way for the implementation to
silently drift", so the rename is a first-class layer rather than per-serializer `source=`
plumbing that one forgotten field would break.

**Why a serializer mixin and not a global renderer.** A renderer-level rename rewrites every key
in the response, including keys that must stay verbatim:

  - `error.details[].field` names the client's *own submitted* field (API §4.2). Rewriting it
    would tell the client a field it never sent was invalid.
  - GeoJSON's `type` / `geometry` / `coordinates` / `properties` are fixed by RFC 7946 and by
    §4.3's `FeatureCollection` payloads. A renderer cannot tell them apart from domain fields.

Applying the rename at the serializer boundary keeps it to fields this project defines.
"""

from __future__ import annotations

import re
from typing import Any

from rest_framework import serializers

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


class CamelCaseSerializer(CamelCaseSerializerMixin, serializers.Serializer[Any]):
    """Plain (non-model) serializer with the rename applied."""


class CamelCaseModelSerializer(CamelCaseSerializerMixin, serializers.ModelSerializer[Any]):
    """Model serializer with the rename applied. The default base for API resources."""
