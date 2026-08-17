"""Issue clustering, severity, confirmations and triage mutations (T4.4-T5.7)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from urbenmend.api.exceptions import Conflict, UnprocessableEntity
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import (
    AuthorizationError,
    has_category_scope,
    require_category_scope,
    require_role,
)
from urbenmend.issues.models import Confirmation, Issue, IssueStatus, StatusEvent
from urbenmend.issues.selectors import active_clustering_rule, matching_open_issues
from urbenmend.notifications.services import record_issue_status_changed
from urbenmend.reporting.models import SEVERITY_RANK, Report, ReportStatus, SeveritySignal

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

logger = structlog.get_logger(__name__)

# The Issue states which can still accept corroborating Reports. Resolved/closed and every
# terminal/moderation branch are intentionally absent.
OPEN_ISSUE_STATUSES = frozenset(
    {
        IssueStatus.SUBMITTED,
        IssueStatus.TRIAGED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.INSUFFICIENT_INFO,
    }
)

# The authoritative PRD section 6.3 graph. Moderation states are intentionally absent: hiding
# and removal are Admin moderation actions, not authority workflow transitions. Reopen is also
# absent because it creates a new linked Issue instead of changing the original row's status.
ISSUE_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    IssueStatus.SUBMITTED: frozenset({IssueStatus.TRIAGED}),
    IssueStatus.TRIAGED: frozenset(
        {
            IssueStatus.ACKNOWLEDGED,
            IssueStatus.REJECTED,
            IssueStatus.DUPLICATE,
            IssueStatus.INSUFFICIENT_INFO,
        }
    ),
    IssueStatus.ACKNOWLEDGED: frozenset({IssueStatus.IN_PROGRESS}),
    IssueStatus.IN_PROGRESS: frozenset({IssueStatus.RESOLVED}),
    IssueStatus.RESOLVED: frozenset({IssueStatus.CLOSED}),
    IssueStatus.CLOSED: frozenset(),
    IssueStatus.REJECTED: frozenset(),
    IssueStatus.DUPLICATE: frozenset(),
    IssueStatus.INSUFFICIENT_INFO: frozenset(),
    IssueStatus.HIDDEN: frozenset(),
    IssueStatus.REMOVED: frozenset(),
}

REOPEN_ACTION = "reopen"
REOPENABLE_ISSUE_STATUSES = frozenset({IssueStatus.RESOLVED, IssueStatus.CLOSED})
REASON_REQUIRED_TRANSITIONS = frozenset(
    {
        IssueStatus.REJECTED,
        IssueStatus.DUPLICATE,
        IssueStatus.INSUFFICIENT_INFO,
        REOPEN_ACTION,
    }
)


@dataclass(frozen=True)
class IssueTransitionPlan:
    """Validated lifecycle intent consumed by the T5.2 mutation service.

    `creates_new_issue` is true only for `reopen`. That action never writes `"reopen"` into the
    status column and never reactivates the original Issue (DM-Q8).
    """

    from_status: str
    to_status: str
    reason: str | None
    creates_new_issue: bool


@dataclass(frozen=True)
class IssueStatusResult:
    """The Issue resource identity returned by the T5.2 status endpoint."""

    issue_id: UUID
    status: str
    duplicate_of_issue_id: UUID | None
    reopened_from_issue_id: UUID | None


@dataclass(frozen=True)
class IssueAssignmentResult:
    """The assignment sub-resource returned by the T5.4 endpoint."""

    issue_id: UUID
    assignee_id: UUID | None


@dataclass(frozen=True)
class IssueSeverityResult:
    """Computed and overridden severity state returned by the T5.5 endpoint."""

    issue_id: UUID
    computed: str
    computed_rationale: str
    overridden: str
    current: str
    override_reason: str
    overridden_by: UUID
    overridden_at: datetime


@dataclass(frozen=True)
class IssueMergeResult:
    """The surviving Issue state returned after a T5.6 merge."""

    issue_id: UUID
    merged_issue_id: UUID
    status: str
    computed_severity: str
    current_severity: str
    report_count: int
    corroboration_count: int


@dataclass(frozen=True)
class SplitIssueState:
    """One side of a T5.7 split response."""

    issue_id: UUID
    status: str
    computed_severity: str
    current_severity: str
    report_count: int
    corroboration_count: int


@dataclass(frozen=True)
class IssueSplitResult:
    """The updated original and newly-created Issue after a T5.7 split."""

    original: SplitIssueState
    created: SplitIssueState


def validate_issue_transition(
    *,
    from_status: str,
    to_status: str,
    reason: str | None = None,
) -> IssueTransitionPlan:
    """Validate one Issue lifecycle intent against BR-16/C-7.

    Illegal edges (including same-state writes and moderation-state changes) are `409
    INVALID_TRANSITION`. Rejected, duplicate, insufficient-info and reopen require a non-blank
    reason and fail as `422` business-rule violations when it is absent. Reopen is valid only
    from resolved/closed and is returned as a create-new-Issue plan; callers must leave the
    original row untouched.
    """
    normalized_reason = reason.strip() if reason is not None else None
    if not normalized_reason:
        normalized_reason = None

    is_reopen = to_status == REOPEN_ACTION
    if is_reopen:
        valid = from_status in REOPENABLE_ISSUE_STATUSES
    else:
        valid = to_status in ISSUE_STATUS_TRANSITIONS.get(from_status, frozenset())

    if not valid:
        raise Conflict(
            f"Issue cannot transition from {from_status!r} to {to_status!r}.",
            code="INVALID_TRANSITION",
        )

    if to_status in REASON_REQUIRED_TRANSITIONS and normalized_reason is None:
        raise UnprocessableEntity(
            f"A reason is required when transitioning to {to_status!r}.",
            code="VALIDATION_FAILED",
        )

    return IssueTransitionPlan(
        from_status=from_status,
        to_status=to_status,
        reason=normalized_reason,
        creates_new_issue=is_reopen,
    )


def _status_result(issue: Issue) -> IssueStatusResult:
    return IssueStatusResult(
        issue_id=issue.pk,
        status=issue.status,
        duplicate_of_issue_id=issue.duplicate_of_id,
        reopened_from_issue_id=issue.reopened_from_id,
    )


def _locked_issue(issue_id: UUID | str) -> Issue:
    try:
        return Issue.objects.select_for_update().select_related("primary_category").get(pk=issue_id)
    except (Issue.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("Issue not found.") from exc


def _locked_assignee(assignee_id: UUID | str) -> User:
    try:
        return User.objects.select_for_update().get(pk=assignee_id)
    except (User.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("Assignee not found.") from exc


@transaction.atomic
def assign_issue(
    *,
    actor: User,
    issue_id: UUID | str,
    assignee_id: UUID | str | None,
) -> IssueAssignmentResult:
    """Assign or unassign one Issue under the T5.4 role and scope rules.

    Authorities may assign only themselves and may clear only their own assignment. Admins may
    assign or clear any Issue, but an assigned Authority must still be active and scoped to the
    Issue category; otherwise the assignment would create work the target cannot legally access.
    """
    require_role(actor, Role.AUTHORITY, Role.ADMIN)
    issue = _locked_issue(issue_id)
    require_category_scope(actor, issue.primary_category)

    if assignee_id is None:
        if actor.role == Role.AUTHORITY and issue.assignee_id not in {None, actor.pk}:
            raise AuthorizationError("You do not have permission to change this assignment.")
        issue.assignee = None
    else:
        if actor.role == Role.AUTHORITY and str(assignee_id) != str(actor.pk):
            raise AuthorizationError("You do not have permission to assign another authority.")
        assignee = _locked_assignee(assignee_id)
        if assignee.role != Role.AUTHORITY or not assignee.is_active:
            raise UnprocessableEntity(
                "The assignee must be an active Authority.",
                code="VALIDATION_FAILED",
            )
        if not has_category_scope(assignee, issue.primary_category):
            raise UnprocessableEntity(
                "The assignee is outside the Issue category scope.",
                code="VALIDATION_FAILED",
            )
        issue.assignee = assignee

    issue.save(update_fields=["assignee", "updated_at"])
    return IssueAssignmentResult(issue_id=issue.pk, assignee_id=issue.assignee_id)


@transaction.atomic
def override_issue_severity(
    *,
    actor: User,
    issue_id: UUID | str,
    severity: str | None,
    reason: str | None,
) -> IssueSeverityResult:
    """Set an explainable authority override while preserving computed severity (T5.5)."""
    require_role(actor, Role.AUTHORITY, Role.ADMIN)
    issue = _locked_issue(issue_id)
    require_category_scope(actor, issue.primary_category)

    if severity not in SeveritySignal.values:
        raise UnprocessableEntity(
            "Severity must be one of critical, high, medium or low.",
            code="VALIDATION_FAILED",
        )
    normalized_reason = reason.strip() if reason is not None else ""
    if not normalized_reason:
        raise UnprocessableEntity(
            "A reason is required to override severity.",
            code="VALIDATION_FAILED",
        )

    overridden_at = timezone.now()
    issue.overridden_severity = severity
    issue.severity_override_reason = normalized_reason
    issue.severity_overridden_by = actor
    issue.severity_overridden_at = overridden_at
    issue.save(
        update_fields=[
            "overridden_severity",
            "severity_override_reason",
            "severity_overridden_by",
            "severity_overridden_at",
            "updated_at",
        ]
    )
    return IssueSeverityResult(
        issue_id=issue.pk,
        computed=issue.computed_severity,
        computed_rationale=issue.computed_severity_rationale,
        overridden=severity,
        current=issue.current_severity,
        override_reason=normalized_reason,
        overridden_by=actor.pk,
        overridden_at=overridden_at,
    )


def _locked_merge_pair(survivor_id: UUID | str, absorbed_id: UUID | str) -> tuple[Issue, Issue]:
    try:
        survivor_uuid = UUID(str(survivor_id))
        absorbed_uuid = UUID(str(absorbed_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Http404("Issue not found.") from exc
    if survivor_uuid == absorbed_uuid:
        raise Conflict("An Issue cannot be merged with itself.", code="INVALID_MERGE")

    issues = {
        issue.pk: issue
        for issue in Issue.objects.select_for_update()
        .select_related("primary_category")
        .filter(pk__in=[survivor_uuid, absorbed_uuid])
        .order_by("id")
    }
    if len(issues) != 2:
        raise Http404("Issue not found.")
    return issues[survivor_uuid], issues[absorbed_uuid]


@transaction.atomic
def merge_issues(
    *,
    actor: User,
    survivor_issue_id: UUID | str,
    merge_with_issue_id: UUID | str,
    reason: str | None,
) -> IssueMergeResult:
    """Absorb one open Issue into another while preserving the retired row (T5.6)."""
    require_role(actor, Role.AUTHORITY, Role.ADMIN)
    survivor, absorbed = _locked_merge_pair(survivor_issue_id, merge_with_issue_id)
    require_category_scope(actor, survivor.primary_category)
    require_category_scope(actor, absorbed.primary_category)

    normalized_reason = reason.strip() if reason is not None else ""
    if not normalized_reason:
        raise UnprocessableEntity(
            "A reason is required to merge Issues.",
            code="VALIDATION_FAILED",
        )
    if survivor.status not in OPEN_ISSUE_STATUSES or absorbed.status not in OPEN_ISSUE_STATUSES:
        raise Conflict("Only open Issues can be merged.", code="INVALID_MERGE")
    if not survivor.reports.exists() or not absorbed.reports.exists():
        raise Conflict("Both Issues must contain Reports before merging.", code="INVALID_MERGE")

    # One citizen can confirm both clusters before an Authority identifies the duplicate. Remove
    # only that collision, then move every remaining confirmation without violating BR-23.
    existing_citizens = survivor.confirmations.values_list("citizen_id", flat=True)
    absorbed.confirmations.filter(citizen_id__in=existing_citizens).delete()
    absorbed.confirmations.update(issue=survivor)
    absorbed.reports.update(issue=survivor)
    _recompute_issue_severity(survivor)

    from_status = absorbed.status
    absorbed.status = IssueStatus.DUPLICATE
    absorbed.duplicate_of = survivor
    absorbed.save(update_fields=["status", "duplicate_of", "updated_at"])
    event = StatusEvent.objects.create(
        issue=absorbed,
        from_status=from_status,
        to_status=IssueStatus.DUPLICATE,
        actor=actor,
        reason=normalized_reason,
        related_issue=survivor,
    )
    record_issue_status_changed(event)
    logger.info(
        "issue.merged",
        survivor_issue_id=str(survivor.pk),
        absorbed_issue_id=str(absorbed.pk),
        actor_id=str(actor.pk),
    )
    return IssueMergeResult(
        issue_id=survivor.pk,
        merged_issue_id=absorbed.pk,
        status=survivor.status,
        computed_severity=survivor.computed_severity,
        current_severity=survivor.current_severity,
        report_count=survivor.report_count,
        corroboration_count=survivor.corroboration_count,
    )


def _split_state(issue: Issue) -> SplitIssueState:
    return SplitIssueState(
        issue_id=issue.pk,
        status=issue.status,
        computed_severity=issue.computed_severity,
        current_severity=issue.current_severity,
        report_count=issue.report_count,
        corroboration_count=issue.corroboration_count,
    )


@transaction.atomic
def split_issue(
    *,
    actor: User,
    issue_id: UUID | str,
    report_ids: list[UUID],
    reason: str | None,
) -> IssueSplitResult:
    """Move selected Reports into one fresh triaged Issue (T5.7, BR-14/18)."""
    require_role(actor, Role.AUTHORITY, Role.ADMIN)
    original = _locked_issue(issue_id)
    require_category_scope(actor, original.primary_category)

    normalized_reason = reason.strip() if reason is not None else ""
    if not normalized_reason:
        raise UnprocessableEntity(
            "A reason is required to split an Issue.",
            code="VALIDATION_FAILED",
        )
    if original.status not in OPEN_ISSUE_STATUSES:
        raise Conflict("Only an open Issue can be split.", code="INVALID_SPLIT")
    if not report_ids or len(set(report_ids)) != len(report_ids):
        raise UnprocessableEntity(
            "Provide one or more distinct Report IDs.",
            code="VALIDATION_FAILED",
        )

    members = list(
        Report.objects.select_for_update()
        .filter(issue=original)
        .select_related("author")
        .order_by("created_at", "id")
    )
    selected_ids = set(report_ids)
    member_ids = {report.pk for report in members}
    if not selected_ids.issubset(member_ids):
        raise Conflict("Every selected Report must belong to this Issue.", code="INVALID_SPLIT")
    if len(selected_ids) >= len(members):
        raise UnprocessableEntity(
            "A split must leave at least one Report on each Issue.",
            code="VALIDATION_FAILED",
        )

    moved = [report for report in members if report.pk in selected_ids]
    remaining = [report for report in members if report.pk not in selected_ids]
    driver = max(moved, key=_severity_rank)
    severity = driver.severity_signal
    if severity is None:  # Narrowed by `_severity_rank`; retained for static type checking.
        raise ClusteringError(f"Issue member Report {driver.pk} has no severity signal.")

    created = Issue.objects.create(
        primary_category=original.primary_category,
        representative_location=moved[0].location,
        computed_severity=severity,
        computed_severity_rationale=_severity_rationale(driver),
        status=IssueStatus.TRIAGED,
        opened_at=moved[0].created_at,
    )
    Report.objects.filter(pk__in=selected_ids).update(issue=created)

    moved_authors = {report.author_id for report in moved}
    remaining_authors = {report.author_id for report in remaining}
    moved_only_authors = moved_authors - remaining_authors
    original.confirmations.filter(citizen_id__in=moved_only_authors).update(issue=created)

    _recompute_issue_severity(original)
    _recompute_issue_severity(created)
    logger.info(
        "issue.split",
        original_issue_id=str(original.pk),
        created_issue_id=str(created.pk),
        actor_id=str(actor.pk),
        moved_report_count=len(moved),
    )
    return IssueSplitResult(original=_split_state(original), created=_split_state(created))


@transaction.atomic
def transition_issue_status(
    *,
    actor: User,
    issue_id: UUID | str,
    to_status: str,
    reason: str | None = None,
    public_note: str | None = None,
    duplicate_of_issue_id: UUID | str | None = None,
) -> IssueStatusResult:
    """Atomically apply one scoped authority lifecycle action (T5.2, BR-15/16/19/26).

    Reopen creates a fresh triaged Issue linked through `reopened_from`; the historical row and
    its member Reports remain untouched. The Status Event is written in this same transaction.
    """
    require_role(actor, Role.AUTHORITY, Role.ADMIN)
    issue = _locked_issue(issue_id)
    require_category_scope(actor, issue.primary_category)
    plan = validate_issue_transition(
        from_status=issue.status,
        to_status=to_status,
        reason=reason,
    )
    normalized_public_note = public_note.strip() if public_note is not None else ""

    if to_status == IssueStatus.DUPLICATE:
        if duplicate_of_issue_id is None:
            raise ValidationError({"duplicate_of_issue_id": "This field is required."})
        surviving = _locked_issue(duplicate_of_issue_id)
        require_category_scope(actor, surviving.primary_category)
        if surviving.pk == issue.pk or surviving.status in {
            IssueStatus.DUPLICATE,
            IssueStatus.HIDDEN,
            IssueStatus.REMOVED,
        }:
            raise Conflict(
                "The selected Issue cannot be the surviving duplicate target.",
                code="INVALID_TRANSITION",
            )
        issue.status = IssueStatus.DUPLICATE
        issue.duplicate_of = surviving
        issue.save(update_fields=["status", "duplicate_of", "updated_at"])
        event = StatusEvent.objects.create(
            issue=issue,
            from_status=plan.from_status,
            to_status=plan.to_status,
            actor=actor,
            reason=plan.reason or "",
            public_note=normalized_public_note,
            related_issue=surviving,
        )
        record_issue_status_changed(event)
        return _status_result(issue)

    if duplicate_of_issue_id is not None:
        raise ValidationError(
            {"duplicate_of_issue_id": "This field is accepted only for duplicate transitions."}
        )

    if plan.creates_new_issue:
        if Issue.objects.filter(reopened_from=issue).exists():
            raise Conflict("This Issue has already been reopened.", code="INVALID_TRANSITION")
        reopened = Issue.objects.create(
            primary_category=issue.primary_category,
            representative_location=issue.representative_location,
            computed_severity=issue.computed_severity,
            computed_severity_rationale=issue.computed_severity_rationale,
            status=IssueStatus.TRIAGED,
            reopened_from=issue,
        )
        event = StatusEvent.objects.create(
            issue=issue,
            from_status=plan.from_status,
            to_status=REOPEN_ACTION,
            actor=actor,
            reason=plan.reason or "",
            public_note=normalized_public_note,
            related_issue=reopened,
        )
        record_issue_status_changed(event)
        return _status_result(reopened)

    issue.status = plan.to_status
    issue.duplicate_of = None
    issue.save(update_fields=["status", "duplicate_of", "updated_at"])
    event = StatusEvent.objects.create(
        issue=issue,
        from_status=plan.from_status,
        to_status=plan.to_status,
        actor=actor,
        reason=plan.reason or "",
        public_note=normalized_public_note,
    )
    record_issue_status_changed(event)
    return _status_result(issue)


class ClusteringError(RuntimeError):
    """Base failure for a Report that cannot be clustered now."""


class ReportNotFound(ClusteringError):
    """The requested Report id does not identify a persisted row."""


class ReportNotReady(ClusteringError):
    """Classification has not produced all inputs required by clustering."""


@dataclass(frozen=True)
class ConfirmationResult:
    """The confirmation write result rendered by API §6.6."""

    issue_id: UUID
    corroboration_count: int


def _confirmation_target(issue_id: UUID | str, *, for_update: bool) -> Issue:
    """Resolve a public, non-moderated Issue without leaking hidden rows."""
    queryset = Issue.objects.exclude(status__in={IssueStatus.HIDDEN, IssueStatus.REMOVED})
    if for_update:
        queryset = queryset.select_for_update()
    try:
        return queryset.get(pk=issue_id)
    except (Issue.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("Issue not found.") from exc


@transaction.atomic
def confirm_issue(*, actor: User, issue_id: UUID | str) -> ConfirmationResult:
    """Create the actor's one revocable confirmation and return the derived count (BR-22/23)."""
    require_role(actor, Role.CITIZEN)
    issue = _confirmation_target(issue_id, for_update=True)
    if Confirmation.objects.filter(issue=issue, citizen=actor).exists():
        raise Conflict("You have already confirmed this issue.", code="ALREADY_CONFIRMED")

    Confirmation.objects.create(issue=issue, citizen=actor)
    return ConfirmationResult(
        issue_id=issue.pk,
        corroboration_count=issue.corroboration_count,
    )


@transaction.atomic
def withdraw_confirmation(*, actor: User, issue_id: UUID | str) -> None:
    """Revoke the actor's confirmation; absence is a non-disclosing `404` (DM-Q5)."""
    require_role(actor, Role.CITIZEN)
    issue = _confirmation_target(issue_id, for_update=True)
    deleted, _ = Confirmation.objects.filter(issue=issue, citizen=actor).delete()
    if deleted == 0:
        raise Http404("Confirmation not found.")


def _severity_rank(report: Report) -> int:
    """Return BR-11's fixed ordering, rejecting an impossible unclassified member."""
    severity = report.severity_signal
    if severity is None:
        raise ClusteringError(f"Issue member Report {report.pk} has no severity signal.")
    try:
        return SEVERITY_RANK[severity]
    except KeyError as exc:
        raise ClusteringError(
            f"Issue member Report {report.pk} has unknown severity {severity!r}."
        ) from exc


def _severity_rationale(report: Report) -> str:
    """Use the classifier explanation, with an identifiable fallback for legacy blank rows."""
    return report.classification_rationale or f"Highest severity supplied by Report {report.pk}."


def _recompute_issue_severity(issue: Issue) -> None:
    """Persist the highest member severity without disturbing an authority override (BR-11/21)."""
    members = list(
        issue.reports.only(
            "id",
            "severity_signal",
            "classification_rationale",
            "created_at",
        ).order_by("created_at", "id")
    )
    if not members:
        raise ClusteringError(f"Issue {issue.pk} has no member Reports.")

    # `max()` keeps the first item on a tie. Oldest Report then UUID gives a stable driver and
    # prevents equal-severity arrivals from changing the displayed rationale on every retry.
    driver = max(members, key=_severity_rank)
    severity = driver.severity_signal
    if severity is None:  # Narrowed by `_severity_rank`; retained for static type checking.
        raise ClusteringError(f"Issue member Report {driver.pk} has no severity signal.")
    rationale = _severity_rationale(driver)

    if issue.computed_severity == severity and issue.computed_severity_rationale == rationale:
        return
    issue.computed_severity = severity
    issue.computed_severity_rationale = rationale
    issue.save(
        update_fields=[
            "computed_severity",
            "computed_severity_rationale",
            "updated_at",
        ]
    )


def _encode_geohash(*, longitude: float, latitude: float, precision: int) -> str:
    """Encode a WGS84 point without adding a runtime dependency for one small primitive."""
    alphabet = "0123456789bcdefghjkmnpqrstuvwxyz"
    longitude_range = [-180.0, 180.0]
    latitude_range = [-90.0, 90.0]
    bits = (16, 8, 4, 2, 1)
    even_bit = True
    bit_index = 0
    character = 0
    encoded: list[str] = []

    while len(encoded) < precision:
        value_range = longitude_range if even_bit else latitude_range
        value = longitude if even_bit else latitude
        midpoint = (value_range[0] + value_range[1]) / 2
        if value >= midpoint:
            character |= bits[bit_index]
            value_range[0] = midpoint
        else:
            value_range[1] = midpoint
        even_bit = not even_bit

        if bit_index < 4:
            bit_index += 1
        else:
            encoded.append(alphabet[character])
            bit_index = 0
            character = 0

    return "".join(encoded)


def _cell_spans_degrees(*, precision: int) -> tuple[float, float]:
    """Longitude/latitude span of every cell at a geohash precision."""
    total_bits = precision * 5
    longitude_bits = (total_bits + 1) // 2
    latitude_bits = total_bits // 2
    return 360.0 / (2**longitude_bits), 180.0 / (2**latitude_bits)


def _geohash_precision(*, latitude: float, radius_m: float) -> int:
    """Choose cells at least `radius_m` wide and high at this latitude."""
    metres_per_degree = 111_320.0
    longitude_scale = max(math.cos(math.radians(latitude)), 0.01)
    for precision in range(12, 0, -1):
        longitude_span, latitude_span = _cell_spans_degrees(precision=precision)
        width_m = longitude_span * metres_per_degree * longitude_scale
        height_m = latitude_span * metres_per_degree
        if min(width_m, height_m) >= radius_m:
            return precision
    return 1


def _neighboring_geohashes(*, point: Point, radius_m: float) -> tuple[str, ...]:
    """The point's cell plus all neighbors whose Reports could match across a boundary."""
    precision = _geohash_precision(latitude=point.y, radius_m=radius_m)
    longitude_span, latitude_span = _cell_spans_degrees(precision=precision)
    cells = {
        _encode_geohash(
            longitude=((point.x + x_offset + 180.0) % 360.0) - 180.0,
            latitude=max(min(point.y + y_offset, 90.0), -90.0),
            precision=precision,
        )
        for x_offset in (-longitude_span, 0.0, longitude_span)
        for y_offset in (-latitude_span, 0.0, latitude_span)
    }
    return tuple(sorted(cells))


def _acquire_spatial_category_locks(*, category_id: int, point: Point, radius_m: float) -> None:
    """Take deadlock-safe transaction locks for the local geohash neighborhood."""
    cells = _neighboring_geohashes(point=point, radius_m=radius_m)
    with connection.cursor() as cursor:
        for cell in cells:
            # `hashtextextended` supplies a stable signed bigint for pg_advisory_xact_lock. A hash
            # collision only serializes unrelated clusters; it cannot compromise correctness.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"issue-cluster:{category_id}:{cell}"],
            )


@transaction.atomic
def cluster_report(report_id: UUID | str) -> UUID:
    """Attach one classified Report to a matching Issue, or atomically create one (FR-18).

    The Report row lock makes repeated delivery for the same id idempotent. The transaction-scoped
    geohash+category locks serialize different Reports that could match the same real-world Issue.
    """
    try:
        report = Report.objects.select_for_update().get(pk=report_id)
    except (Report.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ReportNotFound(f"Report {report_id!s} does not exist.") from exc

    if report.issue_id is not None:
        return report.issue_id
    if report.status in {ReportStatus.HIDDEN, ReportStatus.REMOVED}:
        raise ReportNotReady("Moderated Reports cannot be clustered.")
    if report.category_id is None or report.severity_signal is None or not report.is_classified:
        raise ReportNotReady("Report classification must complete before clustering.")

    rule = active_clustering_rule(category_id=report.category_id)
    _acquire_spatial_category_locks(
        category_id=report.category_id,
        point=report.location,
        radius_m=rule.radius_m,
    )

    cutoff = report.created_at - timedelta(hours=rule.time_window_hours)
    issue = (
        matching_open_issues(
            category_id=report.category_id,
            point=report.location,
            radius_m=rule.radius_m,
            opened_after=cutoff,
            statuses=OPEN_ISSUE_STATUSES,
        )
        .select_for_update()
        .first()
    )

    created = issue is None
    if issue is None:
        issue = Issue.objects.create(
            primary_category_id=report.category_id,
            representative_location=report.location,
            computed_severity=report.severity_signal,
            computed_severity_rationale=_severity_rationale(report),
            status=IssueStatus.TRIAGED,
        )

    report.issue = issue
    report.status = ReportStatus.TRIAGED
    report.save(update_fields=["issue", "status", "updated_at"])
    _recompute_issue_severity(issue)
    logger.info(
        "issue.report_clustered",
        report_id=str(report.pk),
        issue_id=str(issue.pk),
        category_id=report.category_id,
        issue_created=created,
        computed_severity=issue.computed_severity,
    )
    return issue.pk
