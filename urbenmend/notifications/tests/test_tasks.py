"""T6.2 - outbox relay locking, publication and replay behavior."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from urbenmend.identity.tests.factories import AuthorityFactory
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.services import transition_issue_status
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.notifications.models import OutboxEvent
from urbenmend.notifications.tasks import (
    OUTBOX_CONSUMER_TASK,
    OUTBOX_RELAY_TASK,
    consume_outbox_event,
    relay_outbox,
)

pytestmark = pytest.mark.django_db


def _pending_event() -> OutboxEvent:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)
    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )
    return OutboxEvent.objects.get(aggregate_id=issue.pk)


def test_relay_has_stable_task_names_and_schedule() -> None:
    assert relay_outbox.name == OUTBOX_RELAY_TASK
    assert consume_outbox_event.name == OUTBOX_CONSUMER_TASK


def test_relay_publishes_and_marks_event() -> None:
    event = _pending_event()
    with patch.object(consume_outbox_event, "apply_async") as publish:
        assert relay_outbox.run() == 1

    event.refresh_from_db()
    publish.assert_called_once_with(args=[str(event.pk)], task_id=str(event.pk))
    assert event.published_at is not None
    assert event.attempt_count == 1
    assert event.last_error == ""


def test_relay_does_not_republish_published_event() -> None:
    event = _pending_event()
    event.published_at = timezone.now()
    event.save(update_fields=["published_at"])

    with patch.object(consume_outbox_event, "apply_async") as publish:
        assert relay_outbox.run() == 0

    publish.assert_not_called()


def test_publish_failure_leaves_event_pending_for_replay() -> None:
    event = _pending_event()
    with (
        patch.object(
            consume_outbox_event,
            "apply_async",
            side_effect=RuntimeError("broker unavailable"),
        ),
        pytest.raises(RuntimeError, match="broker unavailable"),
    ):
        relay_outbox.run()

    event.refresh_from_db()
    assert event.published_at is None
    assert event.attempt_count == 0


def test_batch_size_limits_claimed_rows() -> None:
    _pending_event()
    second = _pending_event()
    with patch.object(consume_outbox_event, "apply_async") as publish:
        assert relay_outbox.run(batch_size=1) == 1

    assert publish.call_count == 1
    assert OutboxEvent.objects.filter(published_at__isnull=True).count() == 1
    assert OutboxEvent.objects.filter(pk=second.pk, published_at__isnull=True).exists()
