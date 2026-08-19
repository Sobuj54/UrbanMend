"""
Administration & Moderation — write operations.

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

[doc: Arch §3 (FR-30, FR-31)]
"""

from __future__ import annotations

from typing import Any
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from urbenmend.audit.services import record_event
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role
from urbenmend.moderation.models import ModerationAction


@transaction.atomic
def moderate(*, actor: User, target: Any, action: str, reason: str) -> ModerationAction:
    require_role(actor, Role.ADMIN)
    if action not in ModerationAction.Action.values:
        raise ValueError("Invalid moderation action.")
    reason = reason.strip()
    if not reason:
        raise ValueError("A moderation reason is required.")
    from urbenmend.issues.models import Comment, Issue, IssueStatus
    from urbenmend.media.models import Media, MediaState
    from urbenmend.reporting.models import Report, ReportStatus
    if isinstance(target, Report):
        target.status = ReportStatus.HIDDEN if action == "hide" else ReportStatus.REMOVED
        target.save(update_fields=["status"])
    elif isinstance(target, Issue):
        target.status = IssueStatus.HIDDEN if action == "hide" else IssueStatus.REMOVED
        target.save(update_fields=["status", "updated_at"])
    elif isinstance(target, Media):
        target.state = MediaState.HIDDEN if action == "hide" else MediaState.REMOVED
        target.save(update_fields=["state"])
    elif isinstance(target, Comment):
        target.removed_at = timezone.now()
        target.save(update_fields=["removed_at", "updated_at"])
    else:
        raise ValueError("Unsupported moderation target.")
    event = ModerationAction.objects.create(actor=actor, action=action, reason=reason,
        target_content_type=ContentType.objects.get_for_model(target, for_concrete_model=False),
        target_object_id=str(target.pk))
    record_event(actor=actor, action=f"moderation.{action}", target=target,
        after={"reason": reason, "moderation_action_id": str(event.pk)})
    return event
