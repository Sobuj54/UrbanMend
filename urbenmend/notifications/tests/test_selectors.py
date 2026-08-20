from datetime import timedelta

import pytest
from django.core import management
from django.utils import timezone

from urbenmend.notifications.models import OutboxEvent
from urbenmend.notifications.selectors import outbox_backlog

pytestmark = pytest.mark.django_db


def test_outbox_backlog_reports_count_and_oldest_age() -> None:
    now = timezone.now()
    oldest = OutboxEvent.objects.create(
        event_type="issue.status_changed",
        aggregate_type="issue",
        aggregate_id="00000000-0000-0000-0000-000000000001",
        payload={},
    )
    oldest.occurred_at = now - timedelta(minutes=7)
    oldest.save(update_fields=["occurred_at"])
    newer = OutboxEvent.objects.create(
        event_type="issue.status_changed",
        aggregate_type="issue",
        aggregate_id="00000000-0000-0000-0000-000000000002",
        payload={},
    )
    newer.occurred_at = now - timedelta(minutes=2)
    newer.save(update_fields=["occurred_at"])
    count, age = outbox_backlog()
    assert count == 2
    assert age is not None and age.total_seconds() >= 7 * 60


def test_outbox_status_command_is_empty_safe(capsys) -> None:
    management.call_command("outbox_status")
    assert capsys.readouterr().out.strip() == "pending=0 oldest_age_seconds=0"
