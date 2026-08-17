"""T6.1 - transactional outbox creation for Issue status changes."""

from __future__ import annotations

from typing import Any

import pytest

from urbenmend.api.exceptions import Conflict
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AuthorityFactory
from urbenmend.issues.models import Issue, IssueStatus, StatusEvent
from urbenmend.issues.services import transition_issue_status
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.notifications.models import OutboxEvent
from urbenmend.notifications.services import ISSUE_STATUS_CHANGED

pytestmark = pytest.mark.django_db


def _authority_for(issue: Issue) -> User:
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)
    return actor


def test_status_change_writes_complete_outbox_snapshot() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = _authority_for(issue)

    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
        public_note="A crew has acknowledged the report.",
    )

    status_event = StatusEvent.objects.get()
    outbox = OutboxEvent.objects.get()
    assert outbox.event_type == ISSUE_STATUS_CHANGED
    assert outbox.aggregate_id == issue.pk
    assert outbox.published_at is None
    assert outbox.attempt_count == 0
    assert outbox.payload == {
        "schemaVersion": 1,
        "statusEventId": str(status_event.pk),
        "issueId": str(issue.pk),
        "fromStatus": IssueStatus.TRIAGED,
        "toStatus": IssueStatus.ACKNOWLEDGED,
        "actorId": str(actor.pk),
        "reason": "",
        "publicNote": "A crew has acknowledged the report.",
        "relatedIssueId": None,
    }


def test_duplicate_and_reopen_capture_related_issue() -> None:
    duplicate = IssueFactory.create(status=IssueStatus.TRIAGED)
    survivor = IssueFactory.create(primary_category=duplicate.primary_category)
    actor = _authority_for(duplicate)
    transition_issue_status(
        actor=actor,
        issue_id=duplicate.pk,
        to_status=IssueStatus.DUPLICATE,
        duplicate_of_issue_id=survivor.pk,
        reason="Same hazard.",
    )

    closed = IssueFactory.create(status=IssueStatus.CLOSED, primary_category=duplicate.primary_category)
    reopened = transition_issue_status(
        actor=actor,
        issue_id=closed.pk,
        to_status="reopen",
        reason="The hazard recurred.",
    )

    payloads = list(OutboxEvent.objects.order_by("occurred_at").values_list("payload", flat=True))
    assert payloads[0]["relatedIssueId"] == str(survivor.pk)
    assert payloads[1]["issueId"] == str(closed.pk)
    assert payloads[1]["relatedIssueId"] == str(reopened.issue_id)


def test_invalid_transition_writes_no_outbox_event() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    with pytest.raises(Conflict):
        transition_issue_status(
            actor=_authority_for(issue),
            issue_id=issue.pk,
            to_status=IssueStatus.RESOLVED,
        )

    assert OutboxEvent.objects.count() == 0


def test_outbox_failure_rolls_back_status_and_history(monkeypatch: pytest.MonkeyPatch) -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)

    def fail_create(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(OutboxEvent.objects, "create", fail_create)
    with pytest.raises(RuntimeError, match="outbox insert failed"):
        transition_issue_status(
            actor=_authority_for(issue),
            issue_id=issue.pk,
            to_status=IssueStatus.ACKNOWLEDGED,
        )

    issue.refresh_from_db()
    assert issue.status == IssueStatus.TRIAGED
    assert StatusEvent.objects.count() == 0
    assert OutboxEvent.objects.count() == 0
