from typing import cast
from uuid import UUID
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status
from urbenmend.api.pagination import StandardCursorPagination
from urbenmend.geo import selectors
from urbenmend.geo.reference_services import create_poi, update_poi
from urbenmend.geo.reference_services import replace_city_boundary
from urbenmend.geo.serializers import POISerializer, POICreateSerializer, POIUpdateSerializer, POIQuerySerializer, CityBoundarySerializer, CityBoundaryWriteSerializer
from urbenmend.identity.models import User

class POICollectionView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request: Request) -> Response:
        params = POIQuerySerializer(data=request.query_params); params.is_valid(raise_exception=True)
        values = params.validated_data
        queryset = selectors.list_pois(poi_type=values.get("type"), bbox=values.get("bbox"), near=values.get("near"), radius_m=values.get("radius_m"))
        paginator = StandardCursorPagination(); page = paginator.paginate_queryset(queryset, request, view=self) or []
        return paginator.get_paginated_response(POISerializer(page, many=True).data)
    def post(self, request: Request) -> Response:
        serializer = POICreateSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        poi = create_poi(actor=cast("User", request.user), **serializer.validated_data)
        return Response(POISerializer(poi).data, status=status.HTTP_201_CREATED)

class POIDetailView(APIView):
    permission_classes = [IsAuthenticated]
    def patch(self, request: Request, poi_id: UUID) -> Response:
        serializer = POIUpdateSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        return Response(POISerializer(update_poi(actor=cast("User", request.user), poi_id=poi_id, **serializer.validated_data)).data)
    def delete(self, request: Request, poi_id: UUID) -> Response:
        poi = update_poi(actor=cast("User", request.user), poi_id=poi_id, active=False)
        return Response(POISerializer(poi).data)

class CityBoundaryView(APIView):
    permission_classes = [AllowAny]
    def get(self, request: Request) -> Response:
        return Response(CityBoundarySerializer(selectors.active_city_boundary()).data)
    def put(self, request: Request) -> Response:
        serializer = CityBoundaryWriteSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        boundary = replace_city_boundary(actor=cast("User", request.user), **serializer.validated_data)
        return Response(CityBoundarySerializer(boundary).data)
