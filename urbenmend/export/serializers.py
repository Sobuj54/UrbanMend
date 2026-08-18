"""Export API serializers."""

from typing import Any

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
