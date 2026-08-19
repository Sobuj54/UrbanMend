from __future__ import annotations

from typing import Any
from rest_framework import serializers
from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.classification.models import Category

class CategoryLabelSerializer(CamelCaseSerializer):
    en = serializers.CharField(max_length=100)
    bn = serializers.CharField(max_length=100)

class CategorySerializer(CamelCaseSerializer):
    key = serializers.CharField(source="slug", read_only=True)
    label = serializers.SerializerMethodField()
    active = serializers.SerializerMethodField()
    def get_label(self, obj: Category) -> dict[str, str]:
        return {"en": obj.name_en, "bn": obj.name_bn}
    def get_active(self, obj: Category) -> bool:
        return obj.status == "active"

class CategoryCreateSerializer(CamelCaseSerializer):
    key = serializers.SlugField(max_length=50)
    label = CategoryLabelSerializer()
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs

class CategoryUpdateSerializer(CamelCaseSerializer):
    label = CategoryLabelSerializer(required=False)
    active = serializers.BooleanField(required=False)
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        if not attrs:
            raise serializers.ValidationError("Provide label or active.")
        return attrs
