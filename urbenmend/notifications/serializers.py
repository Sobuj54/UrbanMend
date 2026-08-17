"""Notification API serializers (T6.4)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.notifications.models import Notification, NotificationType


class NotificationSerializer(CamelCaseSerializer):
    """The API section 6.11 notification resource."""

    id = serializers.UUIDField(read_only=True)
    type = serializers.CharField(source="notification_type", read_only=True)
    issue_id = serializers.UUIDField(read_only=True)
    body = serializers.CharField(read_only=True)
    channel = serializers.CharField(read_only=True)
    read = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)

    def get_read(self, obj: Notification) -> bool:
        return obj.read_at is not None


class NotificationListQuerySerializer(CamelCaseSerializer):
    """Validated filters while leaving cursor mechanics to the paginator."""

    unread = serializers.BooleanField(required=False)
    type = serializers.MultipleChoiceField(
        choices=NotificationType.choices,
        required=False,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self, extra_allowed=("limit", "cursor"))
        return attrs


class NotificationReadSerializer(CamelCaseSerializer):
    """PATCH body; notifications cannot be made unread through this endpoint."""

    read = serializers.BooleanField(required=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        if attrs["read"] is not True:
            raise serializers.ValidationError(
                {"read": "This endpoint only marks notifications as read."},
                code="VALIDATION_FAILED",
            )
        return attrs


class NotificationReadAllSerializer(CamelCaseSerializer):
    """An explicitly empty POST body; unknown fields must not be silently accepted."""

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs
