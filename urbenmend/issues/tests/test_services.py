"""T4.4 Issue clustering service behavior beyond the parallel race test."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from urbenmend.classification.models import Category
from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.issues.services import (
    ReportNotFound,
    ReportNotReady,
    _cell_spans_degrees,
    _geohash_precision,
    _neighboring_geohashes,
    cluster_report,
)
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.reporting.models import ReportStatus
from urbenmend.reporting.tests.factories import ClassifiedReportFactory, ReportFactory

pytestmark = pytest.mark.django_db

CENTRE = Point(90.4125, 23.8103, srid=4326)
WITHIN_RULE = Point(90.4128, 23.8103, srid=4326)
OUTSIDE_RULE = Point(90.4140, 23.8103, srid=4326)


def test_matching_open_issue_is_reused_and_report_is_triaged() -> None:
    issue = IssueFactory.create(representative_location=CENTRE, status=IssueStatus.ACKNOWLEDGED)
    report = ClassifiedReportFactory.create(location=WITHIN_RULE, category=issue.primary_category)

    issue_id = cluster_report(report.pk)
    report.refresh_from_db()

    assert issue_id == issue.pk
    assert report.issue == issue
    assert report.status == ReportStatus.TRIAGED
    assert Issue.objects.count() == 1


def test_no_match_creates_an_issue_from_the_report() -> None:
    report = ClassifiedReportFactory.create(location=CENTRE)

    issue_id = cluster_report(report.pk)
    issue = Issue.objects.get(pk=issue_id)

    assert issue.primary_category == report.category
    assert issue.representative_location.equals_exact(report.location)
    assert issue.computed_severity == report.severity_signal
    assert issue.computed_severity_rationale == report.classification_rationale
    assert issue.status == IssueStatus.TRIAGED
    assert issue.reports.get() == report


def test_different_category_does_not_match() -> None:
    IssueFactory.create(representative_location=CENTRE)
    lighting = Category.objects.get(slug="street_lighting")
    report = ClassifiedReportFactory.create(category=lighting, location=CENTRE)

    cluster_report(report.pk)

    assert Issue.objects.count() == 2


def test_outside_rule_radius_does_not_match() -> None:
    existing = IssueFactory.create(representative_location=CENTRE)
    report = ClassifiedReportFactory.create(
        category=existing.primary_category,
        location=OUTSIDE_RULE,
    )

    cluster_report(report.pk)

    assert Issue.objects.count() == 2


def test_issue_older_than_rule_window_does_not_match() -> None:
    existing = IssueFactory.create(
        representative_location=CENTRE,
        opened_at=timezone.now() - timedelta(hours=73),
    )
    report = ClassifiedReportFactory.create(
        category=existing.primary_category,
        location=CENTRE,
    )

    cluster_report(report.pk)

    assert Issue.objects.count() == 2


@pytest.mark.parametrize(
    "status",
    [
        IssueStatus.RESOLVED,
        IssueStatus.CLOSED,
        IssueStatus.REJECTED,
        IssueStatus.DUPLICATE,
        IssueStatus.HIDDEN,
        IssueStatus.REMOVED,
    ],
)
def test_terminal_issue_does_not_accept_a_report(status: str) -> None:
    existing = IssueFactory.create(representative_location=CENTRE, status=status)
    report = ClassifiedReportFactory.create(
        category=existing.primary_category,
        location=CENTRE,
    )

    cluster_report(report.pk)

    assert Issue.objects.count() == 2


def test_repeated_delivery_is_idempotent() -> None:
    report = ClassifiedReportFactory.create(location=CENTRE)

    first = cluster_report(report.pk)
    second = cluster_report(report.pk)

    assert second == first
    assert Issue.objects.count() == 1
    assert Issue.objects.get().reports.count() == 1


def test_unclassified_report_is_not_ready() -> None:
    report = ReportFactory.create()

    with pytest.raises(ReportNotReady, match="classification"):
        cluster_report(report.pk)

    assert Issue.objects.count() == 0


@pytest.mark.parametrize("status", [ReportStatus.HIDDEN, ReportStatus.REMOVED])
def test_moderated_report_is_not_clustered(status: str) -> None:
    report = ClassifiedReportFactory.create(status=status)

    with pytest.raises(ReportNotReady, match="Moderated"):
        cluster_report(report.pk)

    assert Issue.objects.count() == 0


def test_missing_report_raises_a_domain_error() -> None:
    missing = uuid.uuid4()

    with pytest.raises(ReportNotFound, match=str(missing)):
        cluster_report(missing)


def test_malformed_report_id_raises_a_domain_error() -> None:
    with pytest.raises(ReportNotFound, match="not-a-uuid"):
        cluster_report("not-a-uuid")


def test_nearby_points_across_a_geohash_boundary_share_a_lock_cell() -> None:
    """Neighbor locking closes the race a single-cell advisory lock would leave."""
    precision = _geohash_precision(latitude=CENTRE.y, radius_m=50)
    longitude_span, _ = _cell_spans_degrees(precision=precision)
    boundary_number = round((CENTRE.x + 180.0) / longitude_span)
    boundary = -180.0 + boundary_number * longitude_span
    west = Point(boundary - 0.0001, CENTRE.y, srid=4326)
    east = Point(boundary + 0.0001, CENTRE.y, srid=4326)

    west_cells = set(_neighboring_geohashes(point=west, radius_m=50))
    east_cells = set(_neighboring_geohashes(point=east, radius_m=50))

    assert west_cells != east_cells
    assert west_cells & east_cells
