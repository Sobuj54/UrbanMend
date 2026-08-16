"""T5.6 - atomic Issue merge and member re-attribution."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from urbenmend.api.exceptions import Conflict, UnprocessableEntity
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.issues.models import Confirmation, Issue, IssueStatus, StatusEvent
from urbenmend.issues.services import merge_issues
from urbenmend.issues.tests.factories import ConfirmationFactory, IssueFactory
from urbenmend.reporting.models import SeveritySignal
from urbenmend.reporting.tests.factories import ClassifiedReportFactory

pytestmark = pytest.mark.django_db


def _issue_with_report(**kwargs: Any) -> Issue:
    issue = IssueFactory.create(**kwargs)
    ClassifiedReportFactory.create(
        issue=issue,
        category=issue.primary_category,
        severity_signal=issue.computed_severity,
        classification_rationale=issue.computed_severity_rationale,
    )
    return issue


def _scoped_authority(*issues: Issue) -> User:
    authority = AuthorityFactory.create()
    authority.category_scope.add(*(issue.primary_category for issue in issues))
    return authority


def _url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-merge", kwargs={"issue_id": issue_id})


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_merge_moves_reports_deduplicates_confirmations_and_recomputes_severity() -> None:
    survivor = _issue_with_report(computed_severity=SeveritySignal.LOW)
    absorbed = _issue_with_report(
        primary_category=survivor.primary_category,
        computed_severity=SeveritySignal.CRITICAL,
        computed_severity_rationale="Live wire above the footpath.",
    )
    shared = UserFactory.create()
    unique = UserFactory.create()
    ConfirmationFactory.create(issue=survivor, citizen=shared)
    ConfirmationFactory.create(issue=absorbed, citizen=shared)
    ConfirmationFactory.create(issue=absorbed, citizen=unique)

    result = merge_issues(
        actor=_scoped_authority(survivor, absorbed),
        survivor_issue_id=survivor.pk,
        merge_with_issue_id=absorbed.pk,
        reason="Both clusters describe the same live wire.",
    )

    survivor.refresh_from_db()
    absorbed.refresh_from_db()
    assert result.issue_id == survivor.pk
    assert survivor.reports.count() == 2
    assert absorbed.reports.count() == 0
    assert survivor.computed_severity == SeveritySignal.CRITICAL
    assert survivor.computed_severity_rationale == "Live wire above the footpath."
    assert set(survivor.confirmations.values_list("citizen_id", flat=True)) == {shared.pk, unique.pk}
    assert Confirmation.objects.count() == 2
    assert absorbed.status == IssueStatus.DUPLICATE
    assert absorbed.duplicate_of == survivor


def test_merge_preserves_survivor_override_and_records_status_event() -> None:
    survivor = _issue_with_report(
        overridden_severity=SeveritySignal.MEDIUM,
        severity_override_reason="Site inspection.",
    )
    absorbed = _issue_with_report(primary_category=survivor.primary_category)
    actor = _scoped_authority(survivor, absorbed)

    result = merge_issues(
        actor=actor,
        survivor_issue_id=survivor.pk,
        merge_with_issue_id=absorbed.pk,
        reason="  Duplicate cluster.  ",
    )

    survivor.refresh_from_db()
    assert survivor.overridden_severity == SeveritySignal.MEDIUM
    assert result.current_severity == SeveritySignal.MEDIUM
    event = StatusEvent.objects.get(issue=absorbed)
    assert event.to_status == IssueStatus.DUPLICATE
    assert event.related_issue == survivor
    assert event.reason == "Duplicate cluster."


@pytest.mark.parametrize("terminal", [IssueStatus.RESOLVED, IssueStatus.CLOSED, IssueStatus.DUPLICATE])
def test_terminal_issue_cannot_be_merged(terminal: str) -> None:
    survivor = _issue_with_report()
    absorbed = _issue_with_report(primary_category=survivor.primary_category, status=terminal)
    with pytest.raises(Conflict) as raised:
        merge_issues(
            actor=AdminFactory.create(),
            survivor_issue_id=survivor.pk,
            merge_with_issue_id=absorbed.pk,
            reason="Duplicate.",
        )
    assert raised.value.get_codes() == "INVALID_MERGE"


def test_merge_rejects_self_missing_reason_role_and_scope() -> None:
    survivor = _issue_with_report()
    absorbed = _issue_with_report(primary_category=survivor.primary_category)
    actor = _scoped_authority(survivor, absorbed)

    with pytest.raises(Conflict):
        merge_issues(
            actor=actor,
            survivor_issue_id=survivor.pk,
            merge_with_issue_id=survivor.pk,
            reason="Self.",
        )
    with pytest.raises(UnprocessableEntity):
        merge_issues(
            actor=actor,
            survivor_issue_id=survivor.pk,
            merge_with_issue_id=absorbed.pk,
            reason="  ",
        )
    with pytest.raises(PermissionDenied):
        merge_issues(
            actor=UserFactory.create(),
            survivor_issue_id=survivor.pk,
            merge_with_issue_id=absorbed.pk,
            reason="Citizen cannot merge.",
        )
    with pytest.raises(PermissionDenied):
        merge_issues(
            actor=AuthorityFactory.create(),
            survivor_issue_id=survivor.pk,
            merge_with_issue_id=absorbed.pk,
            reason="Out of scope.",
        )


def test_event_failure_rolls_back_all_merge_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    survivor = _issue_with_report(computed_severity=SeveritySignal.LOW)
    absorbed = _issue_with_report(
        primary_category=survivor.primary_category,
        computed_severity=SeveritySignal.HIGH,
    )

    def fail_create(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(StatusEvent.objects, "create", fail_create)
    with pytest.raises(RuntimeError, match="event insert failed"):
        merge_issues(
            actor=AdminFactory.create(),
            survivor_issue_id=survivor.pk,
            merge_with_issue_id=absorbed.pk,
            reason="Duplicate.",
        )

    survivor.refresh_from_db()
    absorbed.refresh_from_db()
    assert survivor.reports.count() == 1
    assert absorbed.reports.count() == 1
    assert absorbed.status != IssueStatus.DUPLICATE


def test_merge_endpoint_returns_survivor_and_enforces_contract() -> None:
    survivor = _issue_with_report()
    absorbed = _issue_with_report(primary_category=survivor.primary_category)
    client = _client_for(_scoped_authority(survivor, absorbed))

    response = client.post(
        _url(survivor.pk),
        data={"mergeWithIssueId": str(absorbed.pk), "reason": "Same physical issue."},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["issueId"] == str(survivor.pk)
    assert response.json()["mergedIssueId"] == str(absorbed.pk)
    assert response.json()["reportCount"] == 2

    unknown = client.post(
        _url(survivor.pk),
        data={"mergeWithIssueId": str(absorbed.pk), "reason": "Again.", "force": True},
        content_type="application/json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["details"][0]["field"] == "force"


def test_merge_endpoint_requires_authentication_and_csrf() -> None:
    survivor = _issue_with_report()
    absorbed = _issue_with_report(primary_category=survivor.primary_category)
    body = {"mergeWithIssueId": str(absorbed.pk), "reason": "Duplicate."}
    assert Client().post(_url(survivor.pk), data=body, content_type="application/json").status_code == 401

    client = Client(enforce_csrf_checks=True)
    client.force_login(_scoped_authority(survivor, absorbed))
    assert client.post(_url(survivor.pk), data=body, content_type="application/json").status_code == 403
