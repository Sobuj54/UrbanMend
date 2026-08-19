from __future__ import annotations

import pytest
from django.db import DatabaseError, transaction

from urbenmend.audit import selectors, services
from urbenmend.audit.models import AuditEvent
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_record_event_preserves_snapshots_and_resolves_target() -> None:
    actor = AdminFactory()
    target = UserFactory()

    event = services.record_event(
        actor=actor,
        action="identity.role_changed",
        target=target,
        before={"role": "citizen"},
        after={"role": "authority"},
        metadata={"reason": "approved"},
    )

    assert event.target == target
    assert event.before == {"role": "citizen"}
    assert event.after == {"role": "authority"}
    assert event.metadata == {"reason": "approved"}


def test_record_event_rejects_blank_action_and_unsaved_target() -> None:
    actor = AdminFactory()

    with pytest.raises(ValueError, match="blank"):
        services.record_event(actor=actor, action=" ", target=actor)
    with pytest.raises(ValueError, match="saved"):
        services.record_event(actor=actor, action="identity.created", target=UserFactory.build())


def test_authority_sees_only_own_events_and_admin_sees_all() -> None:
    first = AuthorityFactory()
    second = AuthorityFactory()
    admin = AdminFactory()
    services.record_event(actor=first, action="issue.assigned", target=first)
    services.record_event(actor=second, action="issue.assigned", target=second)

    assert list(selectors.list_events(actor=first).values_list("actor_id", flat=True)) == [first.id]
    assert selectors.list_events(actor=admin).count() == 2


def test_audit_event_is_immutable_through_model_methods() -> None:
    actor = AdminFactory()
    event = services.record_event(actor=actor, action="identity.created", target=actor)
    event.action = "changed"

    with pytest.raises(ValueError, match="immutable"):
        event.save()
    with pytest.raises(ValueError, match="immutable"):
        event.delete()


def test_database_trigger_blocks_queryset_update_and_delete() -> None:
    actor = AdminFactory()
    event = services.record_event(actor=actor, action="identity.created", target=actor)

    with pytest.raises(DatabaseError, match="Audit events are immutable"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(action="changed")
    with pytest.raises(DatabaseError, match="Audit events are immutable"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()
