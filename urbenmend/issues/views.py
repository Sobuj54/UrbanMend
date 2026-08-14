"""Thin HTTP endpoints for Issue status and confirmations (T4.7-T5.2)."""

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
    IssueStatusResponseSerializer,
    IssueStatusTransitionSerializer,
)


class IssueStatusView(APIView):
    """`PATCH /issues/{id}/status` (API section 6.5, T5.2)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str) -> Response:
        serializer = IssueStatusTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = services.transition_issue_status(
            actor=cast("User", request.user),
            issue_id=issue_id,
            to_status=data["to_status"],
            reason=data.get("reason"),
            public_note=data.get("public_note"),
            duplicate_of_issue_id=data.get("duplicate_of_issue_id"),
        )
        return Response(IssueStatusResponseSerializer(result).data, status=status.HTTP_200_OK)


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
