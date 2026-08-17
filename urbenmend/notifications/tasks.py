"""Celery outbox relay (T6.2)."""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from urbenmend.notifications.models import OutboxEvent
from urbenmend.notifications.services import generate_status_change_notifications

logger = structlog.get_logger(__name__)

OUTBOX_RELAY_TASK = "notifications.relay_outbox"
OUTBOX_CONSUMER_TASK = "notifications.consume_outbox_event"
DEFAULT_BATCH_SIZE = 100


@shared_task(
    name=OUTBOX_CONSUMER_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def consume_outbox_event(event_id: str, **_options: Any) -> None:
    """Generate idempotent notification records for one published event (T6.3)."""
    try:
        event = OutboxEvent.objects.get(pk=event_id)
    except OutboxEvent.DoesNotExist:
        logger.warning("notifications.outbox_missing", event_id=event_id)
        return

    created = generate_status_change_notifications(event)
    logger.info("notifications.outbox_consumed", event_id=event_id, notifications_created=created)


@shared_task(name=OUTBOX_RELAY_TASK)
def relay_outbox(*, batch_size: int = DEFAULT_BATCH_SIZE, **_options: Any) -> int:
    """Publish pending outbox rows and mark only successfully published rows.

    The row lock is held across broker publication and the database update. A crash after broker
    publication but before commit leaves the row pending, which intentionally permits a duplicate
    publish; consumers must use the event UUID as their idempotency key.
    """
    if batch_size < 1:
        return 0

    published = 0
    with transaction.atomic():
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(published_at__isnull=True)
            .order_by("occurred_at", "id")[:batch_size]
        )
        for event in events:
            # Importing the task object directly would create a second registration path. The
            # explicit name is the wire contract and keeps task renames from stranding messages.
            consume_outbox_event.apply_async(
                args=[str(event.pk)],
                task_id=str(event.pk),
            )
            event.published_at = timezone.now()
            event.attempt_count += 1
            event.last_error = ""
            event.save(update_fields=["published_at", "attempt_count", "last_error"])
            published += 1

    logger.info("notifications.outbox_relayed", published=published)
    return published
