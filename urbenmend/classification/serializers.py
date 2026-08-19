from __future__ import annotations

from typing import Any
from rest_framework import serializers
from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.classification.models import Category, SeverityKeyword, SeverityKeywordStatus
from urbenmend.reporting.models import SeveritySignal

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

class SeverityKeywordSerializer(CamelCaseSerializer):
    id = serializers.IntegerField(read_only=True)
    term = serializers.CharField(read_only=True)
    language = serializers.CharField(read_only=True)
    severity = serializers.CharField(read_only=True)
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True, allow_null=True)
    active = serializers.SerializerMethodField()
    def get_active(self, obj: SeverityKeyword) -> bool: return obj.status == SeverityKeywordStatus.ACTIVE

class SeverityKeywordWriteSerializer(CamelCaseSerializer):
    term = serializers.CharField(min_length=2, max_length=100, required=False)
    language = serializers.ChoiceField(choices=("en", "bn"), required=False)
    severity = serializers.ChoiceField(choices=SeveritySignal.choices, required=False)
    category = serializers.SlugField(required=False, allow_null=True)
    active = serializers.BooleanField(required=False)
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        if not attrs: raise serializers.ValidationError("Provide at least one field.")
        return attrs
