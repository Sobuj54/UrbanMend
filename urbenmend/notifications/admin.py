"""Operational admin visibility for notification infrastructure."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.notifications.models import Notification, OutboxEvent

if TYPE_CHECKING:
    _OutboxEventAdminBase = admin.ModelAdmin[OutboxEvent]
    _NotificationAdminBase = admin.ModelAdmin[Notification]
else:
    _OutboxEventAdminBase = admin.ModelAdmin
    _NotificationAdminBase = admin.ModelAdmin


@admin.register(OutboxEvent)
class OutboxEventAdmin(_OutboxEventAdminBase):
    """Show relay state without allowing operators to manufacture or rewrite events."""

    list_display = (
        "id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "occurred_at",
        "published_at",
        "attempt_count",
    )
    list_filter = ("event_type", "aggregate_type", "published_at")
    search_fields = ("id", "aggregate_id")
    readonly_fields = (
        "id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "payload",
        "occurred_at",
        "published_at",
        "attempt_count",
        "last_error",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: OutboxEvent | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: OutboxEvent | None = None) -> bool:
        return False


@admin.register(Notification)
class NotificationAdmin(_NotificationAdminBase):
    """Inspect notification fan-out while keeping delivery state service-owned."""

    list_display = (
        "id",
        "recipient",
        "issue",
        "notification_type",
        "channel",
        "state",
        "read_at",
        "created_at",
    )
    list_filter = ("notification_type", "channel", "state")
    search_fields = ("id", "recipient__email", "recipient__phone", "issue__id", "body")
    readonly_fields = [field.name for field in Notification._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Notification | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Notification | None = None) -> bool:
        return False
