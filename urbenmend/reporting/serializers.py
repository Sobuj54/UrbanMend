"""
Reporting — request/response shapes for `POST /reports` (T2.2).

⚠️ **The serializer validates *shape*; `services.py` enforces the *rules*** (FR-3, Arch §3.1).
BR-2/BR-3/BR-35 and the Citizen-only check all live in `submit_report()`, so a management command
or a future bulk importer gets them too. What is duplicated here is deliberate defence in depth:
a missing `location` is a field-level `400` with `details[].field == "location"` (API §4.1), which
is a better answer than the service's generic message — and the service still refuses `None`.

[doc: API §6.3, §1.2, §4.1; FR-5, FR-11, FR-12; BR-2, BR-3, C-2]
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.contrib.gis.geos import Point
from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, to_camel_case


class LocationSerializer(CamelCaseSerializer):
    """`{"lng": 90.399, "lat": 23.777}` (API §1.2 — coordinates are always this object).

    ⚠️ **Bounded to the WGS84 ranges, and that is not an invented policy number** — it is the
    definition of a degree coordinate. Without the bounds a `lat` of `200` builds a valid GEOS
    `Point`, falls outside the boundary polygon, and comes back as `422 OUT_OF_CITY` — telling a
    client with a transposed lat/lng that UrbanMend does not serve their city, which sends them
    looking in exactly the wrong place. Impossible coordinates are a malformed body (`400`); real
    coordinates elsewhere on Earth are the business-rule `422`. Same distinction T2.1 draws
    between `ReportValidationError` and `OutOfCity`.
    """

    lng = serializers.FloatField(min_value=-180.0, max_value=180.0)
    lat = serializers.FloatField(min_value=-90.0, max_value=90.0)

    @staticmethod
    def to_point(data: Mapping[str, float]) -> Point:
        """Build the SRID-4326 `Point` the model column expects.

        ⚠️ **`Point(lng, lat)` — x is longitude.** GeoJSON, PostGIS and GEOS all order `(x, y)`,
        while humans say "lat, long". Transposing them produces a structurally perfect point that
        lands in the Indian Ocean, so every submission is rejected `422 OUT_OF_CITY` and no test of
        shape, SRID or index would notice. `geo/tests/test_city_boundary.py` records the same trap
        on the boundary side.

        A `staticmethod` over a dict, not an instance method: a nested serializer never has its own
        `validated_data` populated, so reading `self.validated_data` here would raise `AssertionError`
        at runtime — the kind of bug a view-level "just build the Point inline" workaround invites,
        which is how the order ends up duplicated in two places.
        """
        return Point(data["lng"], data["lat"], srid=4326)


class ReportSubmitSerializer(CamelCaseSerializer):
    """`POST /reports` request body (API §6.3).

    ⚠️ **A plain `Serializer`, never a `ModelSerializer` over `Report`.** A model serializer's
    field set follows the model, so `status`, `severity_signal`, `confidence`,
    `classification_source` and `classified_at` would each be one `fields` edit — or one
    `"__all__"` — away from being client-settable. Every one of those is derived data that
    api-conventions.md makes read-only to all clients. Same reasoning T1.9 recorded for
    `ProfileUpdateSerializer`.

    ⚠️ **`description` is uncapped here on purpose.** No doc fixes a maximum, and inventing one
    would start rejecting legitimate Bangla reports (which say more per character, the same reason
    `MIN_DESCRIPTION_LENGTH` is low). The real bound is Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` on
    the whole body; submission abuse is FR-33/T2.9's rate limit, not a field length.
    """

    description = serializers.CharField(required=False, allow_blank=True, default="")
    location = LocationSerializer()
    # ⚠️ A *hint*, and the field name is `category` because that is what §6.3 sends — but the
    # value is a `slug`, resolved by `_resolve_category()` against the active taxonomy (C-2).
    # `allow_blank=False`: `""` is not "no category", it is a client bug, and coercing it to
    # `None` would hide a broken picker.
    category = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    # ⚠️ Declared even though nothing can be attached yet (T2.4 uploads, T2.6 attaches) —
    # see `validate_media_ids()` for why declaring beats dropping.
    media_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    # No default: the view falls back to the author's `preferredLanguage` (FR-12), which is a
    # better answer for a Bangla-preferring citizen than the model's `"en"`.
    language = serializers.ChoiceField(choices=["en", "bn"], required=False)

    def validate_media_ids(self, value: list[Any]) -> list[Any]:
        """Refuse a non-empty `mediaIds` rather than dropping it (T2.4/T2.6 not built).

        ⚠️ **Silently ignoring it is the failure mode this rejects.** With the field undeclared,
        DRF drops unknown keys, `media_count` stays `0`, and a photo-only submission gets BR-3's
        `"Add a photo, or describe the problem…"` — an error about `description` when the client
        did attach a photo. The citizen has no way to act on that.

        The message is truthful rather than diplomatic: `POST /media` (§6.4) does not exist yet, so
        **no id a client could send can resolve**. An empty list is accepted, so a client that
        always sends the key is not punished for it.
        """
        if value:
            raise serializers.ValidationError(
                "Media upload is not available yet; submit a description instead.",
                code="NOT_SUPPORTED",
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Reject fields this endpoint does not own.

        ⚠️ **Unknown fields are refused, not ignored — and here the neighbours are the reason.**
        The fields adjacent to this body are `severity_signal`, `status`, `confidence` and
        `issueId`: all derived, all read-only to every client (api-conventions.md). DRF's default
        would answer `POST {"severity":"critical"}` with a `202`, and the citizen would believe
        they had filed a Critical report. That is the same silent-success shape T1.9 refused on
        `PATCH /users/me`.

        The trade-off, stated rather than hidden: a client written against a *newer* additive
        field gets a `400` from this server instead of having it ignored. Additive fields must be
        declared here as they ship, which is required for them to work at all.
        """
        # Both spellings: `CamelCaseSerializerMixin.to_internal_value()` has already rewritten the
        # keys by the time this runs, while `initial_data` keeps the client's originals — so
        # comparing against `self.fields` alone would flag the caller's own `mediaIds` as unknown.
        # ⚠️ `.keys()`, not iteration: DRF's `BindingDict` yields names at runtime but its stubs
        # type `__iter__` as yielding `Field`. Same note as `ProfileUpdateSerializer`.
        declared = {str(name) for name in self.fields.keys()}  # noqa: SIM118
        allowed = declared | {to_camel_case(name) for name in declared}
        submitted = set(self.initial_data) if isinstance(self.initial_data, dict) else set()

        if unknown := sorted(submitted - allowed):
            raise serializers.ValidationError(
                dict.fromkeys(unknown, "This field is not accepted by this endpoint."),
                code="VALIDATION_FAILED",
            )
        return attrs


class ReportSubmitResponseSerializer(CamelCaseSerializer):
    """`202` body for `POST /reports` (API §6.3).

        {"reportId": "…", "status": "processing", "issueId": null,
         "classification": {"state": "pending"}}

    ⚠️ **Serializes `services.SubmissionAcknowledgement`, not a `Report` — T2.3 changed this.**
    §6.3 as amended says an `Idempotency-Key` replay "returns this same `202` body verbatim", and a
    replay has no live row to read: what it has is the acceptance record the original request
    produced. Rendering a `Report` on the fresh path and a stored dict on the replay path would put
    the two bodies in two different pieces of code, free to drift — the client-visible symptom being
    a retry that looks like a *different* submission. One dataclass through one serializer makes them
    identical by construction.

    ⚠️ **The four §6.3 fields, and nothing else.** `SubmissionAcknowledgement` also carries
    `replayed`, which is deliberately not declared here: it is signalled by the
    `Idempotency-Replayed` header (§4.6), and the bodies must stay byte-identical or the header
    would be redundant and the "verbatim" guarantee false. A test asserts the exact key set.

    The derivations these fields used to compute — `issueId` always `null` until T4.5 attaches an
    Issue (BR-6), `classification.state` from `is_classified` rather than a literal `"pending"`
    (BR-9), and `status` read off the row instead of a hardcoded `"processing"` (the T2.2 decision) —
    now live in `services._acknowledge()`, with their reasoning. They moved because they must be
    evaluated **at acceptance time**: by the time a replay arrives, triage may have finished, and
    re-deriving them then would answer a `202` with post-acceptance state that §6.3 sends clients to
    `GET /reports/{id}` for.
    """

    # `CharField`, not `UUIDField(source="pk")`: the acknowledgement already holds a `str`, which is
    # also what survives a round-trip through the idempotency cache. A `UUID` there would be a type
    # the contract does not describe and JSON cannot carry.
    report_id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    # Nullable output: DRF's `Serializer.to_representation` emits `None` without calling the field,
    # so no `allow_null` is needed and `str(None)` can never leak as `"None"`.
    issue_id = serializers.CharField(read_only=True)
    classification = serializers.DictField(child=serializers.CharField(), read_only=True)
