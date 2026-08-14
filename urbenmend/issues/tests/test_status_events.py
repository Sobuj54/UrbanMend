"""T5.3 - atomic, append-only Issue Status Events (BR-31, C-9)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.contrib import admin
from django.db import DatabaseError, connection, transaction
from django.test import Client, RequestFactory
from django.urls import reverse

from urbenmend.api.exceptions import Conflict
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AuthorityFactory
from urbenmend.issues.models import Issue, IssueStatus, StatusEvent
from urbenmend.issues.services import transition_issue_status
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def _authority_for(issue: Issue) -> User:
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)
    return actor


def _url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-status", kwargs={"issue_id": issue_id})


def test_normal_transition_writes_complete_event() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
        public_note="  A crew has acknowledged the report.  ",
    )

    event = StatusEvent.objects.get()
    assert event.issue == issue
    assert event.from_status == IssueStatus.TRIAGED
    assert event.to_status == IssueStatus.ACKNOWLEDGED
    assert event.actor == actor
    assert event.reason == ""
    assert event.public_note == "A crew has acknowledged the report."
    assert event.related_issue is None


def test_reason_required_branch_persists_the_normalized_reason() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    transition_issue_status(
        actor=_authority_for(issue),
        issue_id=issue.pk,
        to_status=IssueStatus.REJECTED,
        reason="  Outside municipal responsibility.  ",
    )

    assert StatusEvent.objects.get().reason == "Outside municipal responsibility."


def test_duplicate_event_points_to_the_surviving_issue() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    surviving = IssueFactory.create(primary_category=issue.primary_category)
    actor = _authority_for(issue)

    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.DUPLICATE,
        duplicate_of_issue_id=surviving.pk,
        reason="Same physical hazard.",
    )

    event = StatusEvent.objects.get()
    assert event.issue == issue
    assert event.to_status == IssueStatus.DUPLICATE
    assert event.related_issue == surviving


def test_reopen_event_stays_on_history_and_points_to_successor() -> None:
    original = IssueFactory.create(status=IssueStatus.CLOSED)
    actor = _authority_for(original)

    result = transition_issue_status(
        actor=actor,
        issue_id=original.pk,
        to_status="reopen",
        reason="The hazard recurred.",
    )

    successor = Issue.objects.get(pk=result.issue_id)
    event = StatusEvent.objects.get()
    assert event.issue == original
    assert event.from_status == IssueStatus.CLOSED
    assert event.to_status == "reopen"
    assert event.related_issue == successor
    assert original.status == IssueStatus.CLOSED


def test_invalid_transition_writes_no_event() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    with pytest.raises(Conflict):
        transition_issue_status(
            actor=_authority_for(issue),
            issue_id=issue.pk,
            to_status=IssueStatus.RESOLVED,
        )

    assert StatusEvent.objects.count() == 0


def test_event_failure_rolls_back_the_status_change(monkeypatch: pytest.MonkeyPatch) -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    def fail_create(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(StatusEvent.objects, "create", fail_create)

    with pytest.raises(RuntimeError, match="event insert failed"):
        transition_issue_status(
            actor=_authority_for(issue),
            issue_id=issue.pk,
            to_status=IssueStatus.ACKNOWLEDGED,
        )

    issue.refresh_from_db()
    assert issue.status == IssueStatus.TRIAGED


def test_event_model_rejects_instance_update_and_delete() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    transition_issue_status(
        actor=_authority_for(issue),
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )
    event = StatusEvent.objects.get()

    event.reason = "rewritten"
    with pytest.raises(ValueError, match="immutable"):
        event.save()
    with pytest.raises(ValueError, match="immutable"):
        event.delete()


def test_database_trigger_rejects_queryset_update_and_delete() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    transition_issue_status(
        actor=_authority_for(issue),
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )
    event = StatusEvent.objects.get()

    with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
        StatusEvent.objects.filter(pk=event.pk).update(reason="rewritten")
    with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
        StatusEvent.objects.filter(pk=event.pk).delete()

    assert StatusEvent.objects.filter(pk=event.pk).exists()


def test_immutable_trigger_exists_in_postgres() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'issues_status_event'::regclass AND NOT tgisinternal"
        )
        names = {row[0] for row in cursor.fetchall()}

    assert "issues_status_event_immutable" in names


def test_status_endpoint_persists_public_note() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    client = Client()
    client.force_login(_authority_for(issue))

    response = client.patch(
        _url(issue.pk),
        data={
            "toStatus": "acknowledged",
            "publicNote": "Work has been scheduled.",
        },
        content_type="application/json",
    )

    assert response.status_code == 200
    assert StatusEvent.objects.get().public_note == "Work has been scheduled."


def test_status_event_admin_is_read_only() -> None:
    model_admin = admin.site._registry[StatusEvent]
    request = RequestFactory().get("/admin/issues/statusevent/")

    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False
