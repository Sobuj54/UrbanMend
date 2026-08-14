"""T5.2 - scoped Issue status mutation and reopen/duplicate actions."""

from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from urbenmend.api.exceptions import Conflict, UnprocessableEntity
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.issues.services import transition_issue_status
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.reporting.tests.factories import ClassifiedReportFactory

pytestmark = pytest.mark.django_db


def _authority_for(issue: Issue) -> User:
    authority = AuthorityFactory.create()
    authority.category_scope.add(issue.primary_category)
    return authority


def _url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-status", kwargs={"issue_id": issue_id})


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_authority_can_advance_a_scoped_issue() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    result = transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )

    issue.refresh_from_db()
    assert result.issue_id == issue.pk
    assert result.status == IssueStatus.ACKNOWLEDGED
    assert issue.status == IssueStatus.ACKNOWLEDGED


def test_admin_can_advance_without_category_scope() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    result = transition_issue_status(
        actor=AdminFactory.create(),
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )

    assert result.status == IssueStatus.ACKNOWLEDGED


def test_citizen_cannot_mutate_status() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    with pytest.raises(PermissionDenied):
        transition_issue_status(
            actor=UserFactory.create(),
            issue_id=issue.pk,
            to_status=IssueStatus.ACKNOWLEDGED,
        )

    issue.refresh_from_db()
    assert issue.status == IssueStatus.TRIAGED


def test_authority_outside_category_scope_cannot_mutate_status() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    with pytest.raises(PermissionDenied):
        transition_issue_status(
            actor=AuthorityFactory.create(),
            issue_id=issue.pk,
            to_status=IssueStatus.ACKNOWLEDGED,
        )


def test_missing_reason_is_rejected_before_mutation() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    with pytest.raises(UnprocessableEntity):
        transition_issue_status(
            actor=actor,
            issue_id=issue.pk,
            to_status=IssueStatus.REJECTED,
            reason="  ",
        )

    issue.refresh_from_db()
    assert issue.status == IssueStatus.TRIAGED


def test_duplicate_transition_links_to_a_scoped_surviving_issue() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    surviving = IssueFactory.create(primary_category=issue.primary_category)
    actor = _authority_for(issue)
    actor.category_scope.add(surviving.primary_category)

    result = transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.DUPLICATE,
        duplicate_of_issue_id=surviving.pk,
        reason="Same pothole cluster.",
    )

    issue.refresh_from_db()
    assert result.status == IssueStatus.DUPLICATE
    assert issue.duplicate_of_id == surviving.pk


def test_duplicate_transition_requires_a_surviving_target() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    with pytest.raises(ValidationError):
        transition_issue_status(
            actor=actor,
            issue_id=issue.pk,
            to_status=IssueStatus.DUPLICATE,
            reason="Same cluster.",
        )


def test_duplicate_target_cannot_be_the_same_or_moderated_issue() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    with pytest.raises(Conflict):
        transition_issue_status(
            actor=actor,
            issue_id=issue.pk,
            to_status=IssueStatus.DUPLICATE,
            duplicate_of_issue_id=issue.pk,
            reason="Self-link should fail.",
        )

    retired = IssueFactory.create(
        status=IssueStatus.REMOVED, primary_category=issue.primary_category
    )
    with pytest.raises(Conflict):
        transition_issue_status(
            actor=actor,
            issue_id=issue.pk,
            to_status=IssueStatus.DUPLICATE,
            duplicate_of_issue_id=retired.pk,
            reason="Retired target should fail.",
        )


def test_reopen_creates_one_linked_triaged_issue_and_preserves_history() -> None:
    issue = IssueFactory.create(status=IssueStatus.RESOLVED)
    report = ClassifiedReportFactory.create(issue=issue)
    actor = _authority_for(issue)

    result = transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status="reopen",
        reason="The hazard has recurred.",
    )

    issue.refresh_from_db()
    reopened = Issue.objects.get(pk=result.issue_id)
    assert issue.status == IssueStatus.RESOLVED
    assert issue.reopened_as == reopened
    assert reopened.status == IssueStatus.TRIAGED
    assert reopened.reopened_from_id == issue.pk
    assert reopened.reports.count() == 0
    assert report.issue_id == issue.pk


def test_reopen_is_single_successor() -> None:
    issue = IssueFactory.create(status=IssueStatus.CLOSED)
    actor = _authority_for(issue)
    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status="reopen",
        reason="Recurrence.",
    )

    with pytest.raises(Conflict):
        transition_issue_status(
            actor=actor,
            issue_id=issue.pk,
            to_status="reopen",
            reason="Another recurrence.",
        )


def test_status_endpoint_returns_camel_case_resource() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    response = _client_for(actor).patch(
        _url(issue.pk),
        data={"toStatus": "acknowledged"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {
        "issueId": str(issue.pk),
        "status": "acknowledged",
        "duplicateOfIssueId": None,
        "reopenedFromIssueId": None,
    }


def test_status_endpoint_requires_reason_and_rejects_unknown_fields() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)
    client = _client_for(actor)

    missing_reason = client.patch(
        _url(issue.pk),
        data={"toStatus": "rejected"},
        content_type="application/json",
    )
    assert missing_reason.status_code == 422

    unknown = client.patch(
        _url(issue.pk),
        data={"toStatus": "acknowledged", "status": "resolved"},
        content_type="application/json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["details"][0]["field"] == "status"


def test_status_endpoint_returns_invalid_transition_for_a_skipped_state() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    response = _client_for(_authority_for(issue)).patch(
        _url(issue.pk),
        data={"toStatus": "in_progress"},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"


def test_status_endpoint_enforces_role_and_category_scope() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    body = {"toStatus": "acknowledged"}

    citizen = _client_for(UserFactory.create()).patch(
        _url(issue.pk), data=body, content_type="application/json"
    )
    out_of_scope = _client_for(AuthorityFactory.create()).patch(
        _url(issue.pk), data=body, content_type="application/json"
    )

    assert citizen.status_code == 403
    assert out_of_scope.status_code == 403


def test_reopen_endpoint_returns_the_new_linked_issue() -> None:
    original = IssueFactory.create(status=IssueStatus.RESOLVED)
    response = _client_for(_authority_for(original)).patch(
        _url(original.pk),
        data={"toStatus": "reopen", "reason": "The hazard has recurred."},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["issueId"] != str(original.pk)
    assert body["status"] == "triaged"
    assert body["reopenedFromIssueId"] == str(original.pk)


def test_status_endpoint_requires_authentication_and_csrf() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    assert Client().patch(_url(issue.pk), data={"toStatus": "acknowledged"}).status_code == 401

    client = Client(enforce_csrf_checks=True)
    client.force_login(actor)
    response = client.patch(
        _url(issue.pk),
        data={"toStatus": "acknowledged"},
        content_type="application/json",
    )
    assert response.status_code == 403
