"""Issue status, assignment and confirmation API serializers (T4.7-T5.4)."""

from typing import Any

from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.services import REOPEN_ACTION


class IssueStatusTransitionSerializer(CamelCaseSerializer):
    """`PATCH /issues/{id}/status` request body (API section 6.5)."""

    to_status = serializers.ChoiceField(choices=[*IssueStatus.values, REOPEN_ACTION])
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    public_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    duplicate_of_issue_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        to_status = attrs.get("to_status")
        duplicate_id = attrs.get("duplicate_of_issue_id")
        if to_status == IssueStatus.DUPLICATE and duplicate_id is None:
            raise serializers.ValidationError(
                {"duplicate_of_issue_id": "This field is required for duplicate transitions."}
            )
        if to_status != IssueStatus.DUPLICATE and duplicate_id is not None:
            raise serializers.ValidationError(
                {"duplicate_of_issue_id": "This field is accepted only for duplicate transitions."}
            )
        return attrs


class IssueStatusResponseSerializer(CamelCaseSerializer):
    """Status mutation result; the full Issue read resource lands with T7.3."""

    issue_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=IssueStatus.choices)
    duplicate_of_issue_id = serializers.UUIDField(allow_null=True)
    reopened_from_issue_id = serializers.UUIDField(allow_null=True)


class IssueAssignmentSerializer(CamelCaseSerializer):
    """`PATCH /issues/{id}/assignment` request body (API section 6.5)."""

    assignee_id = serializers.UUIDField(allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class IssueAssignmentResponseSerializer(CamelCaseSerializer):
    """Assignment mutation result; the full Issue resource lands with T7.3."""

    issue_id = serializers.UUIDField()
    assignee_id = serializers.UUIDField(allow_null=True)


class ConfirmationCreateSerializer(CamelCaseSerializer):
    """The endpoint accepts an empty object and refuses invented client-owned fields."""

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class ConfirmationResponseSerializer(CamelCaseSerializer):
    """`201` body after creating one confirmation."""

    issue_id = serializers.UUIDField()
    corroboration_count = serializers.IntegerField(min_value=0)
