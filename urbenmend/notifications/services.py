"""Notification and transactional-outbox write operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import connection

from urbenmend.notifications.models import OutboxEvent

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
