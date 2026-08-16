"""T5.4 - scope-safe Issue assignment and unassignment."""

from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from urbenmend.api.exceptions import UnprocessableEntity
from urbenmend.identity.models import User, UserStatus
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.issues.models import Issue
from urbenmend.issues.services import assign_issue
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def _scoped_authority(issue: Issue) -> User:
    authority = AuthorityFactory.create()
    authority.category_scope.add(issue.primary_category)
    return authority


def _url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-assignment", kwargs={"issue_id": issue_id})


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_authority_can_assign_and_unassign_self() -> None:
    issue = IssueFactory.create()
    authority = _scoped_authority(issue)

    assigned = assign_issue(actor=authority, issue_id=issue.pk, assignee_id=authority.pk)
    issue.refresh_from_db()
    assert assigned.assignee_id == authority.pk
    assert issue.assignee_id == authority.pk

    cleared = assign_issue(actor=authority, issue_id=issue.pk, assignee_id=None)
    issue.refresh_from_db()
    assert cleared.assignee_id is None
    assert issue.assignee_id is None


def test_authority_cannot_assign_or_unassign_another_authority() -> None:
    issue = IssueFactory.create()
    actor = _scoped_authority(issue)
    other = _scoped_authority(issue)

    with pytest.raises(PermissionDenied):
        assign_issue(actor=actor, issue_id=issue.pk, assignee_id=other.pk)

    issue.assignee = other
    issue.save(update_fields=["assignee", "updated_at"])
    with pytest.raises(PermissionDenied):
        assign_issue(actor=actor, issue_id=issue.pk, assignee_id=None)


def test_admin_can_assign_and_clear_any_scoped_authority() -> None:
    issue = IssueFactory.create()
    assignee = _scoped_authority(issue)
    admin = AdminFactory.create()

    assert assign_issue(actor=admin, issue_id=issue.pk, assignee_id=assignee.pk).assignee_id == assignee.pk
    assert assign_issue(actor=admin, issue_id=issue.pk, assignee_id=None).assignee_id is None


@pytest.mark.parametrize("target", ["citizen", "suspended", "out_of_scope"])
def test_invalid_assignee_is_unprocessable(target: str) -> None:
    issue = IssueFactory.create()
    if target == "citizen":
        assignee = UserFactory.create()
    elif target == "suspended":
        assignee = AuthorityFactory.create(status=UserStatus.SUSPENDED)
        assignee.category_scope.add(issue.primary_category)
    else:
        assignee = AuthorityFactory.create()

    with pytest.raises(UnprocessableEntity):
        assign_issue(actor=AdminFactory.create(), issue_id=issue.pk, assignee_id=assignee.pk)


def test_actor_role_and_issue_scope_are_enforced() -> None:
    issue = IssueFactory.create()
    with pytest.raises(PermissionDenied):
        assign_issue(actor=UserFactory.create(), issue_id=issue.pk, assignee_id=None)
    with pytest.raises(PermissionDenied):
        assign_issue(actor=AuthorityFactory.create(), issue_id=issue.pk, assignee_id=None)


def test_assignment_endpoint_returns_camel_case_and_rejects_unknown_fields() -> None:
    issue = IssueFactory.create()
    authority = _scoped_authority(issue)
    client = _client_for(authority)

    response = client.patch(
        _url(issue.pk),
        data={"assigneeId": str(authority.pk)},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json() == {"issueId": str(issue.pk), "assigneeId": str(authority.pk)}

    unknown = client.patch(
        _url(issue.pk),
        data={"assigneeId": None, "departmentId": "invented"},
        content_type="application/json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["details"][0]["field"] == "departmentId"


def test_assignment_endpoint_validates_body_authentication_and_csrf() -> None:
    issue = IssueFactory.create()
    authority = _scoped_authority(issue)

    missing = _client_for(authority).patch(
        _url(issue.pk), data={}, content_type="application/json"
    )
    assert missing.status_code == 400

    assert Client().patch(
        _url(issue.pk), data={"assigneeId": None}, content_type="application/json"
    ).status_code == 401

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(authority)
    assert csrf_client.patch(
        _url(issue.pk), data={"assigneeId": None}, content_type="application/json"
    ).status_code == 403
