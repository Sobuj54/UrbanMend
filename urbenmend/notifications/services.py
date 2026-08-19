"""Notification and transactional-outbox write operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from urbenmend.identity.models import User, UserStatus
from urbenmend.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationPreference,
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
    task as an error. Email is a separate channel row from in-app delivery.
    """
    if event.event_type != ISSUE_STATUS_CHANGED:
        return 0

    payload = event.payload
    issue_id = event.aggregate_id
    from_status = str(payload["fromStatus"])
    to_status = str(payload["toStatus"])
    body = f"Your reported issue status changed from {from_status} to {to_status}."
    recipients = (
        User.objects.filter(
            reports__issue_id=issue_id,
            status__in=[UserStatus.REGISTERED, UserStatus.VERIFIED, UserStatus.ACTIVE],
        )
        .exclude(notification_preference__in_app=False)
        .distinct()
    )
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


@transaction.atomic
def mark_notification_read(*, actor: User, notification_id: UUID | str) -> Notification:
    """Mark one caller-owned notification read without exposing another user's row."""
    try:
        notification = Notification.objects.select_for_update().get(
            pk=notification_id,
            recipient=actor,
        )
    except (Notification.DoesNotExist, ValueError, TypeError) as exc:
        raise Http404("Notification not found.") from exc

    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
    return notification


@transaction.atomic
def mark_all_notifications_read(*, actor: User) -> int:
    """Mark every currently unread notification owned by the caller."""
    return Notification.objects.filter(recipient=actor, read_at__isnull=True).update(
        read_at=timezone.now()
    )


@transaction.atomic
def update_notification_preferences(
    *, actor: User, in_app: bool | None = None, email: bool | None = None
) -> NotificationPreference:
    preference, _ = NotificationPreference.objects.select_for_update().get_or_create(user=actor)
    fields: list[str] = []
    for name, value in (("in_app", in_app), ("email", email)):
        if value is not None:
            setattr(preference, name, value)
            fields.append(name)
    if fields:
        preference.save(update_fields=[*fields, "updated_at"])
    return preference
