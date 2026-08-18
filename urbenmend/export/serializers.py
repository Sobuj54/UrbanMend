"""Export API serializers."""

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.export.models import Export


class ExportCreateSerializer(CamelCaseSerializer):
    resource = serializers.ChoiceField(choices=["issues", "reports"])
    format = serializers.ChoiceField(choices=["csv", "geojson"])
    filters = serializers.DictField(required=False, default=dict)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        allowed = {"from", "category", "bbox"}
        unknown = sorted(set(attrs.get("filters", {})) - allowed)
        if unknown:
            raise serializers.ValidationError(
                {"filters": f"Unknown filters: {', '.join(unknown)}."}
            )
        return attrs


class ExportSerializer(CamelCaseSerializer):
    export_id = serializers.UUIDField(source="id")

    class Meta:
        model = Export
        fields = ["export_id", "state"]


class ExportStatusSerializer(CamelCaseSerializer):
    """Poll response; ready jobs expose a freshly generated short-lived storage URL."""

    state = serializers.CharField()
    download_url = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()

    class Meta:
        model = Export
        fields = ["state", "download_url", "expires_at"]

    def get_download_url(self, export: Export) -> str | None:
        if export.state != "ready" or not export.object_key:
            return None
        return str(default_storage.url(export.object_key))

    def get_expires_at(self, export: Export) -> str | None:
        if export.state != "ready" or not export.object_key:
            return None
        return (timezone.now() + timedelta(seconds=settings.AWS_QUERYSTRING_EXPIRE)).isoformat()
