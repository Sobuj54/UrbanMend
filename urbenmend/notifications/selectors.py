"""Read-only operational selectors for notification infrastructure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from django.db.models import QuerySet
from django.utils import timezone

from urbenmend.identity.models import User
from urbenmend.notifications.models import Notification, NotificationPreference, OutboxEvent


def list_notifications(
    *, actor: User, unread_only: bool | None = None, notification_types: Sequence[str] = ()
) -> QuerySet[Notification]:
    queryset = Notification.objects.filter(recipient=actor).select_related("issue")
    if unread_only is True:
        queryset = queryset.filter(read_at__isnull=True)
    elif unread_only is False:
        queryset = queryset.filter(read_at__isnull=False)
    if notification_types:
        queryset = queryset.filter(notification_type__in=notification_types)
    return queryset.order_by("-created_at", "-pk")


def get_notification_preferences(*, actor: User) -> NotificationPreference:
    preference = NotificationPreference.objects.filter(user=actor).first()
    return preference or NotificationPreference(user=actor)


def outbox_backlog() -> tuple[int, timedelta | None]:
    """Return pending event count and age of the oldest pending event."""
    pending: QuerySet[OutboxEvent] = OutboxEvent.objects.filter(published_at__isnull=True)
    oldest = pending.order_by("occurred_at").values_list("occurred_at", flat=True).first()
    if oldest is None:
        return 0, None
    return pending.count(), timezone.now() - oldest
