"""T6.3 - idempotent in-app notification generation."""

from __future__ import annotations

import pytest

from urbenmend.identity.tests.factories import AuthorityFactory, UserFactory
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.services import transition_issue_status
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationState,
    NotificationType,
    NotificationPreference,
    OutboxEvent,
)
from urbenmend.notifications.services import generate_status_change_notifications
from urbenmend.notifications.tasks import consume_outbox_event
from urbenmend.reporting.tests.factories import ReportFactory

pytestmark = pytest.mark.django_db


def test_status_event_fans_out_once_per_distinct_report_author() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    first = UserFactory.create()
    second = UserFactory.create()
    ReportFactory.create(author=first, issue=issue)
    ReportFactory.create(author=first, issue=issue)
    ReportFactory.create(author=second, issue=issue)
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)

    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )
    event = OutboxEvent.objects.get(aggregate_id=issue.pk)

    assert generate_status_change_notifications(event) == 2
    notifications = Notification.objects.order_by("recipient_id")
    assert notifications.count() == 2
    assert set(notifications.values_list("recipient_id", flat=True)) == {first.pk, second.pk}
    assert set(notifications.values_list("channel", flat=True)) == {NotificationChannel.IN_APP}
    assert set(notifications.values_list("state", flat=True)) == {NotificationState.DELIVERED}
    assert set(notifications.values_list("notification_type", flat=True)) == {
        NotificationType.ISSUE_STATUS_CHANGED
    }


def test_replaying_event_does_not_duplicate_notifications() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    author = UserFactory.create()
    ReportFactory.create(author=author, issue=issue)
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)
    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )
    event = OutboxEvent.objects.get(aggregate_id=issue.pk)

    generate_status_change_notifications(event)
    generate_status_change_notifications(event)

    assert Notification.objects.count() == 1
    assert Notification.objects.get().source_event_id == event.pk


def test_in_app_opt_out_is_honored_during_generation() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    opted_out = UserFactory.create()
    enabled = UserFactory.create()
    ReportFactory.create(author=opted_out, issue=issue)
    ReportFactory.create(author=enabled, issue=issue)
    NotificationPreference.objects.create(user=opted_out, in_app=False)
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)
    transition_issue_status(actor=actor, issue_id=issue.pk, to_status=IssueStatus.ACKNOWLEDGED)

    generate_status_change_notifications(OutboxEvent.objects.get(aggregate_id=issue.pk))

    assert set(Notification.objects.values_list("recipient_id", flat=True)) == {enabled.pk}


def test_celery_consumer_generates_notifications_idempotently() -> None:
    issue = IssueFactory.create(status=IssueStatus.TRIAGED)
    author = UserFactory.create()
    ReportFactory.create(author=author, issue=issue)
    actor = AuthorityFactory.create()
    actor.category_scope.add(issue.primary_category)
    transition_issue_status(
        actor=actor,
        issue_id=issue.pk,
        to_status=IssueStatus.ACKNOWLEDGED,
    )
    event = OutboxEvent.objects.get(aggregate_id=issue.pk)

    consume_outbox_event.run(str(event.pk))
    consume_outbox_event.run(str(event.pk))

    assert Notification.objects.filter(recipient=author, source_event=event).count() == 1


def test_non_status_event_is_ignored() -> None:
    event = OutboxEvent.objects.create(
        event_type="unrelated.event",
        aggregate_type="issue",
        aggregate_id=IssueFactory.create().pk,
        payload={},
    )

    assert generate_status_change_notifications(event) == 0
    assert Notification.objects.count() == 0
