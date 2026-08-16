"""Thin HTTP endpoints for Issue triage mutations and confirmations (T4.7-T5.6)."""

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
    IssueAssignmentResponseSerializer,
    IssueAssignmentSerializer,
    IssueMergeResponseSerializer,
    IssueMergeSerializer,
    IssueSeverityOverrideSerializer,
    IssueSeverityResponseSerializer,
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


class IssueAssignmentView(APIView):
    """`PATCH /issues/{id}/assignment` (API section 6.5, T5.4)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str) -> Response:
        serializer = IssueAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.assign_issue(
            actor=cast("User", request.user),
            issue_id=issue_id,
            assignee_id=serializer.validated_data["assignee_id"],
        )
        return Response(
            IssueAssignmentResponseSerializer(result).data,
            status=status.HTTP_200_OK,
        )


class IssueSeverityView(APIView):
    """`PATCH /issues/{id}/severity` (API section 6.5, T5.5)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, issue_id: str) -> Response:
        serializer = IssueSeverityOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.override_issue_severity(
            actor=cast("User", request.user),
            issue_id=issue_id,
            severity=serializer.validated_data.get("severity"),
            reason=serializer.validated_data.get("reason"),
        )
        return Response(
            IssueSeverityResponseSerializer(result).data,
            status=status.HTTP_200_OK,
        )


class IssueMergeView(APIView):
    """`POST /issues/{id}/merge` (API section 6.5, T5.6)."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, issue_id: str) -> Response:
        serializer = IssueMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.merge_issues(
            actor=cast("User", request.user),
            survivor_issue_id=issue_id,
            merge_with_issue_id=serializer.validated_data["merge_with_issue_id"],
            reason=serializer.validated_data.get("reason"),
        )
        return Response(IssueMergeResponseSerializer(result).data, status=status.HTTP_200_OK)


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
