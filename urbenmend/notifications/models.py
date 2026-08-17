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


class NotificationType(models.TextChoices):
    """The user-facing reason a notification exists."""

    ISSUE_STATUS_CHANGED = "issue_status_changed", _("Issue status changed")


class NotificationChannel(models.TextChoices):
    """Delivery surface for one notification record."""

    IN_APP = "in_app", _("In-app")
    EMAIL = "email", _("Email")
    SMS = "sms", _("SMS")


class NotificationState(models.TextChoices):
    """Delivery outcome for one channel attempt."""

    PENDING = "pending", _("Pending")
    SENT = "sent", _("Sent")
    DELIVERED = "delivered", _("Delivered")
    FAILED = "failed", _("Failed")


class Notification(models.Model):
    """One recipient-visible status notification generated from an outbox event (T6.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        "identity.User",
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    issue = models.ForeignKey(
        "issues.Issue",
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    source_event = models.ForeignKey(
        OutboxEvent,
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    notification_type = models.CharField(max_length=50, choices=NotificationType.choices)
    channel = models.CharField(max_length=16, choices=NotificationChannel.choices)
    body = models.TextField()
    state = models.CharField(
        max_length=16,
        choices=NotificationState.choices,
        default=NotificationState.PENDING,
        db_index=True,
    )
    read_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_notification"
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")
        ordering = ["-created_at", "-id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["source_event", "recipient", "channel"],
                name="notifications_one_channel_per_event_recipient",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["recipient", "-created_at"],
                name="notify_recipient_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.notification_type} for {self.recipient_id}"
