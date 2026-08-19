"""
Audit & Integrity — read operations.

Query functions for this module. Kept separate from services.py so reads never acquire
write-path side effects, and so the modules that consume this one have a single documented
surface to call [doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

[doc: Arch §3 (FR-32)]
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from django.db.models import QuerySet

from urbenmend.audit.models import AuditEvent
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import AuthorizationError


def list_events(
    *,
    actor: User,
    actor_id: UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> QuerySet[AuditEvent]:
    """Admins see all events; authorities see only events they created."""
    if actor.role not in {Role.AUTHORITY, Role.ADMIN}:
        raise AuthorizationError("You do not have permission to view audit events.")
    queryset = AuditEvent.objects.select_related("actor", "target_content_type")
    if actor.role != Role.ADMIN:
        queryset = queryset.filter(actor=actor)
    elif actor_id is not None:
        queryset = queryset.filter(actor_id=actor_id)
    if action:
        queryset = queryset.filter(action=action)
    if target_type:
        queryset = queryset.filter(target_content_type__model=target_type)
    if target_id:
        queryset = queryset.filter(target_object_id=target_id)
    if from_date:
        queryset = queryset.filter(created_at__gte=from_date)
    if to_date:
        queryset = queryset.filter(created_at__lte=to_date)
    return queryset
