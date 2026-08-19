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

from django.db.models import QuerySet

from urbenmend.audit.models import AuditEvent
from urbenmend.identity.models import Role, User


def list_events(*, actor: User) -> QuerySet[AuditEvent]:
    """Admins see all events; authorities see only events they created."""
    queryset = AuditEvent.objects.select_related("actor", "target_content_type")
    if actor.role == Role.ADMIN:
        return queryset
    return queryset.filter(actor=actor)
