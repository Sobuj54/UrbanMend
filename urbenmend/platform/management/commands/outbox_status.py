"""Report the transactional-outbox backlog for operational monitoring."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from urbenmend.notifications.selectors import outbox_backlog


class Command(BaseCommand):
    help = "Report pending outbox events and the oldest event age."

    def handle(self, *args, **options) -> None:
        count, age = outbox_backlog()
        age_seconds = 0 if age is None else max(0, int(age.total_seconds()))
        self.stdout.write(f"pending={count} oldest_age_seconds={age_seconds}")
