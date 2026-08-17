"""Notifications and transactional-outbox persistence (T6.1)."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _


class OutboxEvent(models.Model):
    """A durable domain event awaiting at-least-once dispatch."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=100)
    aggregate_type = models.CharField(max_length=50)
    aggregate_id = models.UUIDField()
    payload = models.JSONField()
    occurred_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "notifications_outbox_event"
        verbose_name = _("outbox event")
        verbose_name_plural = _("outbox events")
        ordering = ["occurred_at", "id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["published_at", "occurred_at"],
                name="notify_outbox_pending_idx",
            ),
            models.Index(
                fields=["aggregate_type", "aggregate_id"],
                name="notify_outbox_aggregate_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.pk})"
