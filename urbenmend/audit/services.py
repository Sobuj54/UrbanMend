"""
Audit & Integrity — write operations.

Every state change and every authorization check for this module lives here. This file
exists from day one even while empty: R-12 is the risk that "service-layer discipline
erodes under Django's idiom, scattering authorization into views/serializers", and the
named mitigation is that the convention is already in place, so putting a rule in a view
is never the path of least resistance.

Rules for this file [doc: Arch §3.1, FR-3]:
  - Callers pass the acting user; functions authorize before mutating. DRF permission
    classes are defence-in-depth, never the enforcement point.
  - Wrap multi-write operations in `transaction.atomic`.
  - Enqueue Celery tasks via `transaction.on_commit` so a worker cannot observe an
    uncommitted row [doc: Arch §2.4, §4.1].
  - Reads belong in selectors.py.

[doc: Arch §3 (FR-32)]
"""

from __future__ import annotations

from typing import Any

from django.contrib.contenttypes.models import ContentType

from urbenmend.audit.models import AuditEvent
from urbenmend.identity.models import User


def record_event(
    *,
    actor: User,
    action: str,
    target: Any,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Persist one immutable audit event for a concrete model instance."""
    if not action.strip():
        raise ValueError("Audit action cannot be blank.")
    if (
        target is None
        or getattr(target, "pk", None) is None
        or getattr(getattr(target, "_state", None), "adding", True)
    ):
        raise ValueError("Audit target must be a saved model instance.")
    return AuditEvent.objects.create(
        actor=actor,
        action=action.strip(),
        target_content_type=ContentType.objects.get_for_model(target, for_concrete_model=False),
        target_object_id=str(target.pk),
        before=before,
        after=after,
        metadata=metadata or {},
    )
