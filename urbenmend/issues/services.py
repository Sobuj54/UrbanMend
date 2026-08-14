"""Concurrency-safe Issue find-or-create clustering (T4.4)."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.issues.selectors import active_clustering_rule, matching_open_issues
from urbenmend.reporting.models import Report, ReportStatus

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


class ClusteringError(RuntimeError):
    """Base failure for a Report that cannot be clustered now."""


class ReportNotFound(ClusteringError):
    """The requested Report id does not identify a persisted row."""


class ReportNotReady(ClusteringError):
    """Classification has not produced all inputs required by clustering."""


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
            computed_severity_rationale=(
                report.classification_rationale
                or f"Initial severity supplied by Report {report.pk}."
            ),
            status=IssueStatus.TRIAGED,
        )

    report.issue = issue
    report.status = ReportStatus.TRIAGED
    report.save(update_fields=["issue", "status", "updated_at"])
    logger.info(
        "issue.report_clustered",
        report_id=str(report.pk),
        issue_id=str(issue.pk),
        category_id=report.category_id,
        issue_created=created,
    )
    return issue.pk
