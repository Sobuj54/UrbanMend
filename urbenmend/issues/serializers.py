"""Confirmation API serializers (T4.7, API §6.6)."""

from typing import Any

from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields


class ConfirmationCreateSerializer(CamelCaseSerializer):
    """The endpoint accepts an empty object and refuses invented client-owned fields."""

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class ConfirmationResponseSerializer(CamelCaseSerializer):
    """`201` body after creating one confirmation."""

    issue_id = serializers.UUIDField()
    corroboration_count = serializers.IntegerField(min_value=0)
