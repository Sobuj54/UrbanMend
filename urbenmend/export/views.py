"""Export create and polling endpoints."""

from typing import cast

from django.http import Http404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.export import selectors, services
from urbenmend.export.serializers import ExportCreateSerializer, ExportSerializer
from urbenmend.identity.models import User
from urbenmend.identity.services import AuthorizationError


class ExportCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = ExportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            export = services.create_export(
                actor=cast("User", request.user),
                resource=data["resource"],
                file_format=data["format"],
                filters=data["filters"],
            )
        except PermissionError as exc:
            raise AuthorizationError(str(exc)) from exc
        return Response(ExportSerializer(export).data, status=status.HTTP_202_ACCEPTED)


class ExportDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, export_id: str) -> Response:
        export = selectors.visible_export(actor=cast("User", request.user), export_id=export_id)
        if export is None:
            raise Http404("Export not found.")
        return Response(ExportSerializer(export).data)
