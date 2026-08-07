"""
camelCase boundary tests (A8, T0.6).

API §1.2 requires camelCase JSON; DRF emits snake_case. The docs call this specific gap "the
single easiest way for the implementation to silently drift" — which is exactly why it is
asserted rather than trusted, and why the negative cases (keys that must NOT be renamed) carry
as much weight as the positive ones.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework import serializers

from urbenmend.api.serializers import (
    CamelCaseSerializer,
    to_camel_case,
    to_snake_case,
)


class ExampleSerializer(CamelCaseSerializer):
    """Multi-word and single-word fields, plus a read-only derived one."""

    report_id = serializers.CharField()
    created_at = serializers.CharField()
    status = serializers.CharField()
    corroboration_count = serializers.IntegerField(read_only=True)


@pytest.mark.parametrize(
    ("snake", "camel"),
    [
        ("report_id", "reportId"),
        ("created_at", "createdAt"),
        ("corroboration_count", "corroborationCount"),
        ("status", "status"),  # Single word — unchanged.
        ("next_cursor", "nextCursor"),
    ],
)
def test_field_names_convert_both_ways(snake: str, camel: str) -> None:
    assert to_camel_case(snake) == camel
    assert to_snake_case(camel) == snake


def test_conversion_is_idempotent() -> None:
    """An already-camelCase name survives a second pass.

    A serializer that declares `reportId` directly must not become `reportid` — the rename is
    applied per-response, and a non-idempotent one corrupts on any repeated application.
    """
    assert to_camel_case("reportId") == "reportId"
    assert to_camel_case(to_camel_case("report_id")) == "reportId"


def test_output_keys_are_camel_case() -> None:
    data = ExampleSerializer(
        {
            "report_id": "r-1",
            "created_at": "2026-08-05T10:15:30Z",
            "status": "submitted",
            "corroboration_count": 3,
        }
    ).data

    assert set(data) == {"reportId", "createdAt", "status", "corroborationCount"}
    assert "report_id" not in data


def test_input_keys_are_accepted_in_camel_case() -> None:
    """A client sends what the contract documents, and validation resolves it.

    Without the inbound half, every camelCase field a client sends would read as missing and the
    request would 400 on fields it actually supplied.
    """
    serializer = ExampleSerializer(
        data={"reportId": "r-1", "createdAt": "2026-08-05T10:15:30Z", "status": "submitted"}
    )

    assert serializer.is_valid(), serializer.errors
    # Validated data is snake_case again — application code keeps Python naming.
    assert serializer.validated_data["report_id"] == "r-1"


def test_a_non_object_body_reaches_drfs_own_validation() -> None:
    """A list or scalar body must 400, not 500.

    `to_internal_value` receives whatever the client sent. Assuming a dict there would raise
    `AttributeError` inside the serializer and surface as an unhandled 500 — a malformed request
    turned into a server error (API §4.2 puts it at 400).
    """
    serializer = ExampleSerializer(data=["not", "an", "object"])
    assert not serializer.is_valid()


def test_geojson_structural_keys_are_untouched() -> None:
    """RFC 7946 / API §4.3 fix `type`, `geometry`, `coordinates`, `properties`.

    This is the reason the rename is a serializer mixin rather than a global renderer: a
    renderer cannot distinguish a GeoJSON structural key from a domain field, and a renamed one
    breaks every mapping client.
    """
    for key in ("type", "geometry", "coordinates", "properties", "features"):
        assert to_camel_case(key) == key


def test_the_envelope_builder_does_no_renaming_of_its_own() -> None:
    """The rename belongs to the serializer, which knows its field names; `exceptions.py` does not.

    `_flatten_validation_detail` walks a detail tree that may contain a carried payload or a
    localized message. Renaming keys there would rewrite content the API is merely transporting,
    which is the same reason the project has no global camelCase renderer.
    """
    from urbenmend.api import exceptions

    assert not hasattr(exceptions, "to_camel_case")


def test_rejected_field_names_are_reported_in_camel_case() -> None:
    """⚠️ `error.details[].field` must name the field the client actually sent (API §4.2).

    DRF raises with its own `snake_case` field names, so without the rename a rejected `createdAt`
    comes back as `created_at` — a field the client never sent, and one it cannot map back to an
    input to highlight. The inbound rename alone leaves the request half-translated.
    """
    serializer = ExampleSerializer(data={"reportId": "r-1", "status": "submitted"})

    assert not serializer.is_valid()
    assert set(serializer.errors) == {"createdAt"}


def test_a_rejected_nested_field_keeps_its_dotted_camel_path() -> None:
    """The `location.lng` path in the envelope has to be camelCase at every segment."""

    class Inner(CamelCaseSerializer):
        max_speed = serializers.IntegerField()

    class Outer(CamelCaseSerializer):
        speed_limit = Inner()

    serializer = Outer(data={"speedLimit": {"maxSpeed": "fast"}})

    assert not serializer.is_valid()
    assert set(serializer.errors) == {"speedLimit"}
    assert set(serializer.errors["speedLimit"]) == {"maxSpeed"}


def test_renaming_an_error_preserves_the_machine_readable_issue_code() -> None:
    """⚠️ The leaf must stay DRF's `ErrorDetail`, not be rebuilt as a plain `str`.

    `_flatten_validation_detail` reads `.code` off the leaf to fill the contract's `issue`
    (`REQUIRED`, `INVALID`). A rename that reconstructed leaves would drop it, and every `issue`
    in the envelope would silently degrade to the `INVALID` fallback.
    """
    serializer = ExampleSerializer(data={"reportId": "r-1", "status": "submitted"})

    assert not serializer.is_valid()
    assert serializer.errors["createdAt"][0].code == "required"


def test_an_object_level_rejection_is_renamed_too() -> None:
    """`validate()` runs after `to_internal_value`, so the funnel has to be `run_validation`.

    Wrapping the narrower method would leave object-level errors in `snake_case` — a half-fix that
    passes every field-level test.
    """

    class Strict(CamelCaseSerializer):
        report_id = serializers.CharField(required=False)

        def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
            raise serializers.ValidationError({"report_id": "Not allowed here."})

    serializer = Strict(data={"reportId": "r-1"})

    assert not serializer.is_valid()
    assert set(serializer.errors) == {"reportId"}


def test_double_underscores_are_left_alone() -> None:
    """A `__` or trailing `_` is a typo worth surfacing, not input to normalize silently."""
    assert to_camel_case("report__id") == "report_Id"
    assert to_camel_case("report_") == "report_"


def test_nested_values_pass_through_unchanged() -> None:
    """Only the serializer's own declared field names are renamed.

    A `JSONField`'s contents or an LLM rationale blob are data, not this project's field names —
    rewriting keys inside them would corrupt payloads the API is merely carrying.
    """

    class WithPayload(CamelCaseSerializer):
        raw_payload = serializers.JSONField()

    data: dict[str, Any] = WithPayload({"raw_payload": {"some_key": 1}}).data
    assert data["rawPayload"] == {"some_key": 1}
