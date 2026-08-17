"""Notification and transactional-outbox write operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import connection, transaction
from django.utils import timezone

from urbenmend.identity.models import User, UserStatus
from urbenmend.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationState,
    NotificationType,
    OutboxEvent,
)

if TYPE_CHECKING:
    from urbenmend.issues.models import StatusEvent

ISSUE_STATUS_CHANGED = "issue.status_changed"
ISSUE_STATUS_CHANGED_SCHEMA_VERSION = 1


def record_issue_status_changed(event: StatusEvent) -> OutboxEvent:
    """Record a status-change snapshot inside the caller's domain transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError("Outbox events must be recorded inside a database transaction.")

    payload: dict[str, Any] = {
        "schemaVersion": ISSUE_STATUS_CHANGED_SCHEMA_VERSION,
        "statusEventId": str(event.pk),
        "issueId": str(event.issue_id),
        "fromStatus": event.from_status,
        "toStatus": event.to_status,
        "actorId": str(event.actor_id),
        "reason": event.reason,
        "publicNote": event.public_note,
        "relatedIssueId": None if event.related_issue_id is None else str(event.related_issue_id),
    }
    return OutboxEvent.objects.create(
        event_type=ISSUE_STATUS_CHANGED,
        aggregate_type="issue",
        aggregate_id=event.issue_id,
        payload=payload,
    )


@transaction.atomic
def generate_status_change_notifications(event: OutboxEvent) -> int:
    """Create one delivered in-app notification per active report author.

    Celery may deliver an outbox message more than once. The database uniqueness constraint,
    together with ``ignore_conflicts``, makes this fan-out idempotent without treating a duplicate
    task as an error. Email and SMS are intentionally separate channel rows in T6.5/T6.6.
    """
    if event.event_type != ISSUE_STATUS_CHANGED:
        return 0

    payload = event.payload
    issue_id = event.aggregate_id
    from_status = str(payload["fromStatus"])
    to_status = str(payload["toStatus"])
    body = f"Your reported issue status changed from {from_status} to {to_status}."
    recipients = User.objects.filter(
        reports__issue_id=issue_id,
        status__in=[UserStatus.REGISTERED, UserStatus.VERIFIED, UserStatus.ACTIVE],
    ).distinct()
    now = timezone.now()
    notifications = [
        Notification(
            recipient=recipient,
            issue_id=issue_id,
            source_event=event,
            notification_type=NotificationType.ISSUE_STATUS_CHANGED,
            channel=NotificationChannel.IN_APP,
            body=body,
            state=NotificationState.DELIVERED,
            sent_at=now,
            delivered_at=now,
        )
        for recipient in recipients
    ]
    Notification.objects.bulk_create(notifications, ignore_conflicts=True)
    return Notification.objects.filter(
        source_event=event,
        channel=NotificationChannel.IN_APP,
    ).count()
