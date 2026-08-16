"""Issue status, assignment, severity, merge/split and confirmation serializers (T4.7-T5.7)."""

from typing import Any

from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer, reject_unknown_fields
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.services import REOPEN_ACTION
from urbenmend.reporting.models import SeveritySignal


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


class IssueSeverityOverrideSerializer(CamelCaseSerializer):
    """`PATCH /issues/{id}/severity` request body (API section 6.5)."""

    # Business-rule errors are 422, so presence/band/reason validation lives in the service.
    severity = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class IssueSeverityStateSerializer(CamelCaseSerializer):
    computed = serializers.ChoiceField(choices=SeveritySignal.choices)
    computed_rationale = serializers.CharField()
    overridden = serializers.ChoiceField(choices=SeveritySignal.choices)
    current = serializers.ChoiceField(choices=SeveritySignal.choices)
    override_reason = serializers.CharField()
    overridden_by = serializers.UUIDField()
    overridden_at = serializers.DateTimeField()


class IssueSeverityResponseSerializer(CamelCaseSerializer):
    """The preserved computed value and current human override (BR-20/21)."""

    issue_id = serializers.UUIDField()
    severity = IssueSeverityStateSerializer(source="*")


class IssueMergeSerializer(CamelCaseSerializer):
    """`POST /issues/{id}/merge` request body (API section 6.5)."""

    merge_with_issue_id = serializers.UUIDField()
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class IssueMergeResponseSerializer(CamelCaseSerializer):
    """Compact surviving Issue resource until the full T7.3 serializer exists."""

    issue_id = serializers.UUIDField()
    merged_issue_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=IssueStatus.choices)
    computed_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    current_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    report_count = serializers.IntegerField(min_value=1)
    corroboration_count = serializers.IntegerField(min_value=0)


class IssueSplitSerializer(CamelCaseSerializer):
    """`POST /issues/{id}/split` request body (API section 6.5)."""

    report_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=True)
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class SplitIssueStateSerializer(CamelCaseSerializer):
    issue_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=IssueStatus.choices)
    computed_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    current_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    report_count = serializers.IntegerField(min_value=1)
    corroboration_count = serializers.IntegerField(min_value=0)


class IssueSplitResponseSerializer(CamelCaseSerializer):
    original = SplitIssueStateSerializer()
    created = SplitIssueStateSerializer()


class ConfirmationCreateSerializer(CamelCaseSerializer):
    """The endpoint accepts an empty object and refuses invented client-owned fields."""

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class ConfirmationResponseSerializer(CamelCaseSerializer):
    """`201` body after creating one confirmation."""

    issue_id = serializers.UUIDField()
    corroboration_count = serializers.IntegerField(min_value=0)
