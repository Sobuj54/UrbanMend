from __future__ import annotations
from typing import Any
from django.conf import settings
from django.contrib.gis.geos import Point, Polygon
from rest_framework import serializers
from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.geo.models import POI, POIType

class POISerializer(CamelCaseSerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    type = serializers.CharField(source="poi_type", read_only=True)
    location = serializers.SerializerMethodField()
    source = serializers.CharField(read_only=True)
    active = serializers.BooleanField(source="is_active", read_only=True)
    def get_location(self, obj: POI) -> dict[str, float]: return {"lng": obj.location.x, "lat": obj.location.y}

class LocationSerializer(CamelCaseSerializer):
    lng = serializers.FloatField(min_value=-180, max_value=180)
    lat = serializers.FloatField(min_value=-90, max_value=90)

class POICreateSerializer(CamelCaseSerializer):
    name = serializers.CharField(max_length=200)
    type = serializers.ChoiceField(choices=POIType.choices)
    location = LocationSerializer()
    source = serializers.CharField(max_length=100)
    def validate(self, attrs): reject_unknown_fields(self); return attrs

class POIUpdateSerializer(CamelCaseSerializer):
    name = serializers.CharField(max_length=200, required=False)
    type = serializers.ChoiceField(choices=POIType.choices, required=False)
    source = serializers.CharField(max_length=100, required=False)
    active = serializers.BooleanField(required=False)
    def validate(self, attrs):
        reject_unknown_fields(self)
        if not attrs: raise serializers.ValidationError("Provide at least one field.")
        return attrs

class POIQuerySerializer(CamelCaseSerializer):
    type = serializers.ChoiceField(choices=POIType.choices, required=False)
    bbox = serializers.CharField(required=False)
    near_lng = serializers.FloatField(required=False, min_value=-180, max_value=180)
    near_lat = serializers.FloatField(required=False, min_value=-90, max_value=90)
    radius_m = serializers.FloatField(required=False, min_value=1, max_value=float(settings.REPORT_SEARCH_MAX_RADIUS_M))
    def validate_bbox(self, value: str) -> Polygon:
        try: parts = [float(item.strip()) for item in value.split(",")]
        except ValueError as exc: raise serializers.ValidationError("Expected minLng,minLat,maxLng,maxLat.") from exc
        if len(parts) != 4: raise serializers.ValidationError("Expected minLng,minLat,maxLng,maxLat.")
        a,b,c,d = parts
        if not (-180 <= a < c <= 180 and -90 <= b < d <= 90): raise serializers.ValidationError("Invalid bbox bounds.")
        box = Polygon.from_bbox(parts); box.srid = 4326; return box
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self, extra_allowed=("limit", "cursor"))
        spatial = [attrs.get("near_lng"), attrs.get("near_lat"), attrs.get("radius_m")]
        if attrs.get("bbox") is not None and any(v is not None for v in spatial): raise serializers.ValidationError({"bbox": "Use bbox or near parameters, not both."})
        if any(v is not None for v in spatial) and not all(v is not None for v in spatial): raise serializers.ValidationError("nearLng, nearLat and radiusM are required together.")
        if all(v is not None for v in spatial): attrs["near"] = Point(attrs.pop("near_lng"), attrs.pop("near_lat"), srid=4326)
        return attrs
