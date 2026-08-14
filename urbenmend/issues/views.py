"""Thin HTTP endpoints for revocable Issue confirmations (T4.7)."""

from typing import cast

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.identity.models import User
from urbenmend.issues import services
from urbenmend.issues.serializers import (
    ConfirmationCreateSerializer,
    ConfirmationResponseSerializer,
)


class IssueConfirmationCreateView(APIView):
    """`POST /issues/{id}/confirmations` (API §6.6)."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, issue_id: str) -> Response:
        serializer = ConfirmationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.confirm_issue(actor=cast("User", request.user), issue_id=issue_id)
        return Response(
            ConfirmationResponseSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )


class IssueConfirmationDeleteView(APIView):
    """`DELETE /issues/{id}/confirmations/me` (API §6.6, DM-Q5)."""

    permission_classes = [IsAuthenticated]

    def delete(self, request: Request, issue_id: str) -> Response:
        services.withdraw_confirmation(actor=cast("User", request.user), issue_id=issue_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
