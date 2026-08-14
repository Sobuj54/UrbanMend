"""Issue clustering, severity, confirmations and lifecycle rules (T4.4-T5.1)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404

from urbenmend.api.exceptions import Conflict, UnprocessableEntity
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role
from urbenmend.issues.models import Confirmation, Issue, IssueStatus
from urbenmend.issues.selectors import active_clustering_rule, matching_open_issues
from urbenmend.reporting.models import SEVERITY_RANK, Report, ReportStatus

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
