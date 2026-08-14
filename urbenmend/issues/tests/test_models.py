"""Schema guarantees for the T4.1 Issue aggregate and Report membership."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.gis.db.models import PointField
from django.contrib.postgres.indexes import GistIndex
from django.db.models import ProtectedError

from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.reporting.models import Report, SeveritySignal
from urbenmend.reporting.tests.factories import ClassifiedReportFactory

pytestmark = pytest.mark.django_db


def test_issue_uses_an_opaque_uuid_and_defaults_to_submitted() -> None:
    issue = IssueFactory.create()

    assert isinstance(issue.pk, uuid.UUID)
    assert issue.status == IssueStatus.SUBMITTED


def test_issue_owns_the_shared_bounded_severity_enum() -> None:
    computed = Issue._meta.get_field("computed_severity")
    overridden = Issue._meta.get_field("overridden_severity")

    assert computed.choices == SeveritySignal.choices
    assert overridden.choices == SeveritySignal.choices


def test_override_changes_current_severity_without_destroying_computed_value() -> None:
    issue = IssueFactory.create(
        computed_severity=SeveritySignal.HIGH,
        overridden_severity=SeveritySignal.LOW,
    )

    assert issue.current_severity == SeveritySignal.LOW
    assert issue.computed_severity == SeveritySignal.HIGH


def test_current_severity_uses_computed_value_without_an_override() -> None:
    issue = IssueFactory.create(computed_severity=SeveritySignal.CRITICAL)

    assert issue.current_severity == SeveritySignal.CRITICAL


def test_report_can_be_unclustered_then_attached_to_exactly_one_issue() -> None:
    report = ClassifiedReportFactory.create()
    first_issue = IssueFactory.create()
    second_issue = IssueFactory.create()

    assert report.issue is None

    report.issue = first_issue
    report.save(update_fields=["issue"])
    report.issue = second_issue
    report.save(update_fields=["issue"])
    report.refresh_from_db()

    assert report.issue == second_issue
    assert first_issue.reports.count() == 0
    assert second_issue.reports.get() == report


def test_report_count_is_derived_from_membership() -> None:
    issue = IssueFactory.create()
    ClassifiedReportFactory.create_batch(2, issue=issue)

    assert issue.report_count == 2
    assert "report_count" not in {field.name for field in Issue._meta.fields}
    assert "corroboration_count" not in {field.name for field in Issue._meta.fields}


def test_issue_with_member_reports_cannot_be_hard_deleted() -> None:
    issue = IssueFactory.create()
    report = ClassifiedReportFactory.create(issue=issue)

    with pytest.raises(ProtectedError):
        issue.delete()

    assert Issue.objects.filter(pk=issue.pk).exists()
    assert Report.objects.filter(pk=report.pk).exists()


def test_representative_location_is_indexed_wgs84_geography() -> None:
    field = Issue._meta.get_field("representative_location")
    indexes = {index.name: index for index in Issue._meta.indexes}

    assert isinstance(field, PointField)
    assert field.geography is True
    assert field.srid == 4326
    assert field.spatial_index is False
    assert isinstance(indexes["issues_issue_location_gist"], GistIndex)
    assert indexes["issues_issue_location_gist"].fields == ["representative_location"]


def test_issue_status_contains_workflow_and_moderation_states_but_not_reopen() -> None:
    values = set(IssueStatus.values)

    assert {
        "submitted",
        "triaged",
        "acknowledged",
        "in_progress",
        "resolved",
        "closed",
        "rejected",
        "duplicate",
        "insufficient_info",
        "hidden",
        "removed",
    } == values
    assert "reopen" not in values
