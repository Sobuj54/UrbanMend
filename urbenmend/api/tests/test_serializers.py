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


def test_error_detail_field_names_are_not_renamed() -> None:
    """`error.details[].field` echoes the client's own submitted field name (API §4.2).

    The error envelope is built in `exceptions.py`, which never routes through this mixin — this
    asserts the separation holds, since renaming there would report a field the client did not
    send.
    """
    from urbenmend.api import exceptions

    assert not hasattr(exceptions, "to_camel_case")


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
