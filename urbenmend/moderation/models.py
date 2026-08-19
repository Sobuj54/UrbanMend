"""
Administration & Moderation — persistence.

Reference-data management (POIs, severity keyword lists), content
moderation, account verification tools.

[doc: Arch §3 (FR-30, FR-31); schema in docs/03-data-model.md]
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class ModerationAction(models.Model):
    class Action(models.TextChoices):
        HIDE = "hide", "Hide"
        REMOVE = "remove", "Remove"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey("identity.User", on_delete=models.PROTECT, related_name="moderation_actions")
    action = models.CharField(max_length=16, choices=Action.choices)
    reason = models.TextField()
    target_content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    target_object_id = models.CharField(max_length=128)
    target = GenericForeignKey("target_content_type", "target_object_id")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "moderation_action"
        ordering = ["-created_at", "-id"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValueError("Moderation actions are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValueError("Moderation actions are immutable.")
