"""Notification read operations (T6.4)."""

from __future__ import annotations

from collections.abc import Sequence

from django.db.models import QuerySet

from urbenmend.identity.models import User
from urbenmend.notifications.models import Notification, NotificationPreference


def list_notifications(
    *,
    actor: User,
    unread_only: bool | None = None,
    notification_types: Sequence[str] = (),
) -> QuerySet[Notification]:
    """Return only the caller's notifications, newest first."""
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
