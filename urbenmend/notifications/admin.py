"""Operational admin visibility for notification infrastructure."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.notifications.models import OutboxEvent

if TYPE_CHECKING:
    _OutboxEventAdminBase = admin.ModelAdmin[OutboxEvent]
else:
    _OutboxEventAdminBase = admin.ModelAdmin


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
