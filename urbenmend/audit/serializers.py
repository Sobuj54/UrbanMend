from __future__ import annotations

from typing import Any

from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.audit.models import AuditEvent


class AuditEventSerializer(CamelCaseSerializer):
    actor_id = serializers.UUIDField(read_only=True)
    action = serializers.CharField(read_only=True)
    target_type = serializers.CharField(source="target_content_type.model", read_only=True)
    target_id = serializers.CharField(source="target_object_id", read_only=True)
    before = serializers.JSONField(read_only=True, allow_null=True)
    after = serializers.JSONField(read_only=True, allow_null=True)
    at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = AuditEvent
        fields = ("actor_id", "action", "target_type", "target_id", "before", "after", "at")


class AuditEventQuerySerializer(CamelCaseSerializer):
    actor_id = serializers.UUIDField(required=False)
    action = serializers.CharField(required=False)
    target_type = serializers.CharField(required=False)
    target_id = serializers.CharField(required=False)
    from_date = serializers.DateTimeField(required=False)
    to_date = serializers.DateTimeField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self, extra_allowed=("limit", "cursor"))
        if (
            attrs.get("from_date")
            and attrs.get("to_date")
            and attrs["from_date"] > attrs["to_date"]
        ):
            raise serializers.ValidationError({"from": "Must be before or equal to `to`."})
        return attrs
