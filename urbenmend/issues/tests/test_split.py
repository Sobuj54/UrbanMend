"""T5.7 - atomic Issue split with report and confirmation attribution."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from urbenmend.api.exceptions import Conflict, UnprocessableEntity
from urbenmend.classification.tests.factories import CategoryFactory
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.issues.services import split_issue
from urbenmend.issues.tests.factories import ConfirmationFactory, IssueFactory
from urbenmend.reporting.models import SeveritySignal
from urbenmend.reporting.tests.factories import ClassifiedReportFactory

pytestmark = pytest.mark.django_db


def _scoped_authority(issue: Issue) -> User:
    authority = AuthorityFactory.create()
    authority.category_scope.add(issue.primary_category)
    return authority


def _url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-split", kwargs={"issue_id": issue_id})


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _split_in_thread(*, actor_id: uuid.UUID, issue_id: uuid.UUID, report_id: uuid.UUID) -> str:
    close_old_connections()
    try:
        split_issue(
            actor=User.objects.get(pk=actor_id),
            issue_id=issue_id,
            report_ids=[report_id],
            reason="Concurrent correction.",
        )
        return "created"
    except Conflict:
        return "conflict"
    finally:
        connection.close()


def test_split_moves_reports_recomputes_both_sides_and_preserves_original_override() -> None:
    issue = IssueFactory.create(
        computed_severity=SeveritySignal.CRITICAL,
        overridden_severity=SeveritySignal.HIGH,
        severity_override_reason="Original site inspection.",
    )
    low = ClassifiedReportFactory.create(
        issue=issue,
        category=issue.primary_category,
        severity_signal=SeveritySignal.LOW,
        classification_rationale="Minor surface crack.",
    )
    critical = ClassifiedReportFactory.create(
        issue=issue,
        category=issue.primary_category,
        severity_signal=SeveritySignal.CRITICAL,
        classification_rationale="Live wire touching the road.",
    )

    result = split_issue(
        actor=_scoped_authority(issue),
        issue_id=issue.pk,
        report_ids=[critical.pk],
        reason="The electrical hazard is at a different location.",
    )

    issue.refresh_from_db()
    created = Issue.objects.get(pk=result.created.issue_id)
    low.refresh_from_db()
    critical.refresh_from_db()
    assert low.issue_id == issue.pk
    assert critical.issue_id == created.pk
    assert issue.computed_severity == SeveritySignal.LOW
    assert issue.current_severity == SeveritySignal.HIGH
    assert created.computed_severity == SeveritySignal.CRITICAL
    assert created.current_severity == SeveritySignal.CRITICAL
    assert created.overridden_severity is None
    assert created.status == IssueStatus.TRIAGED
    assert created.representative_location.equals_exact(critical.location)


def test_confirmations_move_only_for_authors_exclusive_to_moved_side() -> None:
    issue = IssueFactory.create()
    moved_only = UserFactory.create()
    both_sides = UserFactory.create()
    confirmation_only = UserFactory.create()
    moved = ClassifiedReportFactory.create(
        issue=issue, author=moved_only, category=issue.primary_category
    )
    moved_shared = ClassifiedReportFactory.create(
        issue=issue, author=both_sides, category=issue.primary_category
    )
    ClassifiedReportFactory.create(issue=issue, author=both_sides, category=issue.primary_category)
    ConfirmationFactory.create(issue=issue, citizen=moved_only)
    ConfirmationFactory.create(issue=issue, citizen=both_sides)
    ConfirmationFactory.create(issue=issue, citizen=confirmation_only)

    result = split_issue(
        actor=AdminFactory.create(),
        issue_id=issue.pk,
        report_ids=[moved.pk, moved_shared.pk],
        reason="Separate physical defect.",
    )

    created = Issue.objects.get(pk=result.created.issue_id)
    assert set(created.confirmations.values_list("citizen_id", flat=True)) == {moved_only.pk}
    assert set(issue.confirmations.values_list("citizen_id", flat=True)) == {
        both_sides.pk,
        confirmation_only.pk,
    }


@pytest.mark.parametrize("report_ids", [[], None])
def test_split_requires_selected_reports(report_ids: list[uuid.UUID] | None) -> None:
    issue = IssueFactory.create()
    first = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    selected = [] if report_ids is None else report_ids
    with pytest.raises(UnprocessableEntity):
        split_issue(
            actor=AdminFactory.create(),
            issue_id=issue.pk,
            report_ids=selected,
            reason="Separate issue.",
        )
    assert first.issue_id == issue.pk


def test_split_rejects_duplicates_nonmembers_and_emptying_original() -> None:
    issue = IssueFactory.create()
    first = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    second = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    outsider = ClassifiedReportFactory.create(category=issue.primary_category)
    actor = AdminFactory.create()

    with pytest.raises(UnprocessableEntity):
        split_issue(actor=actor, issue_id=issue.pk, report_ids=[first.pk, first.pk], reason="X")
    with pytest.raises(Conflict):
        split_issue(actor=actor, issue_id=issue.pk, report_ids=[outsider.pk], reason="X")
    with pytest.raises(UnprocessableEntity):
        split_issue(actor=actor, issue_id=issue.pk, report_ids=[first.pk, second.pk], reason="X")


@pytest.mark.django_db(transaction=True)
def test_concurrent_repeat_creates_exactly_one_new_issue() -> None:
    issue = IssueFactory.create(primary_category=CategoryFactory.create())
    moved = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    actor = _scoped_authority(issue)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(
            future.result()
            for future in [
                executor.submit(
                    _split_in_thread,
                    actor_id=actor.pk,
                    issue_id=issue.pk,
                    report_id=moved.pk,
                ),
                executor.submit(
                    _split_in_thread,
                    actor_id=actor.pk,
                    issue_id=issue.pk,
                    report_id=moved.pk,
                ),
            ]
        )

    assert outcomes == ["conflict", "created"]
    assert Issue.objects.count() == 2


def test_split_enforces_open_state_reason_role_and_scope() -> None:
    closed = IssueFactory.create(status=IssueStatus.CLOSED)
    report = ClassifiedReportFactory.create(issue=closed, category=closed.primary_category)
    ClassifiedReportFactory.create(issue=closed, category=closed.primary_category)
    with pytest.raises(Conflict):
        split_issue(
            actor=AdminFactory.create(), issue_id=closed.pk, report_ids=[report.pk], reason="X"
        )

    issue = IssueFactory.create()
    moved = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    with pytest.raises(UnprocessableEntity):
        split_issue(
            actor=AdminFactory.create(), issue_id=issue.pk, report_ids=[moved.pk], reason=" "
        )
    with pytest.raises(PermissionDenied):
        split_issue(
            actor=UserFactory.create(), issue_id=issue.pk, report_ids=[moved.pk], reason="X"
        )
    with pytest.raises(PermissionDenied):
        split_issue(
            actor=AuthorityFactory.create(), issue_id=issue.pk, report_ids=[moved.pk], reason="X"
        )


def test_split_endpoint_returns_both_sides_and_enforces_contract() -> None:
    issue = IssueFactory.create()
    moved = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    client = _client_for(_scoped_authority(issue))

    response = client.post(
        _url(issue.pk),
        data={"reportIds": [str(moved.pk)], "reason": "Different physical issue."},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["original"]["issueId"] == str(issue.pk)
    assert body["original"]["reportCount"] == 1
    assert body["created"]["issueId"] != str(issue.pk)
    assert body["created"]["reportCount"] == 1

    unknown = client.post(
        _url(issue.pk),
        data={"reportIds": [str(moved.pk)], "reason": "X", "category": "roads"},
        content_type="application/json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["details"][0]["field"] == "category"


def test_split_endpoint_requires_authentication_and_csrf() -> None:
    issue = IssueFactory.create()
    moved = ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    ClassifiedReportFactory.create(issue=issue, category=issue.primary_category)
    body = {"reportIds": [str(moved.pk)], "reason": "Different issue."}
    assert (
        Client().post(_url(issue.pk), data=body, content_type="application/json").status_code == 401
    )

    client = Client(enforce_csrf_checks=True)
    client.force_login(_scoped_authority(issue))
    assert (
        client.post(_url(issue.pk), data=body, content_type="application/json").status_code == 403
    )
