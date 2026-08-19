"""
Audit & Integrity — persistence.

Append-only audit log writes and queries.

[doc: Arch §3 (FR-32); schema in docs/03-data-model.md]
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class AuditEvent(models.Model):
    """Append-only record of a security or integrity relevant action."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        "identity.User", on_delete=models.PROTECT, related_name="audit_events"
    )
    action = models.CharField(max_length=96, db_index=True)
    target_content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    target_object_id = models.CharField(max_length=128)
    target = GenericForeignKey("target_content_type", "target_object_id")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_event"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["actor", "created_at"], name="audit_actor_created_idx"),
            models.Index(fields=["target_content_type", "target_object_id"], name="audit_target_idx"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValueError("Audit events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValueError("Audit events are immutable.")
