"""Read-only P10 latency smoke checks for representative API query paths."""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.identity.models import User


class Command(BaseCommand):
    help = "Measure representative read-path latency and SQL counts without creating data."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--iterations", type=int, default=5)
        parser.add_argument(
            "--max-ms",
            type=float,
            default=2000.0,
            help="Fail if any representative request exceeds this latency (default: 2000 ms).",
        )
        parser.add_argument(
            "--max-queries",
            type=int,
            default=100,
            help="Fail if any representative request exceeds this SQL count (default: 100).",
        )

    def handle(self, *args, **options) -> None:
        iterations = max(1, options["iterations"])
        max_ms = options["max_ms"]
        max_queries = options["max_queries"]
        if max_ms <= 0 or max_queries < 1:
            raise CommandError("--max-ms must be positive and --max-queries must be at least 1")
        public_client = Client(HTTP_HOST="localhost")
        authenticated_client = APIClient(HTTP_HOST="localhost")
        user = User.objects.filter(
            role="citizen", status__in=["registered", "verified", "active"]
        ).first()
        if user is None:
            raise CommandError("perf_smoke requires at least one active citizen account")
        authenticated_client.force_authenticate(user)
        paths = {
            "reports": (authenticated_client, reverse("api:reports")),
            "issues": (public_client, reverse("api:issues")),
            "map": (
                public_client,
                f"{reverse('api:map-issues')}?bbox=90.40,23.80,90.43,23.83&zoom=8",
            ),
        }
        for name, (client, path) in paths.items():
            elapsed: list[float] = []
            query_counts: list[int] = []
            for _ in range(iterations):
                started = time.perf_counter()
                with CaptureQueriesContext(connection) as queries:
                    response = client.get(path)
                    response.close()
                elapsed.append((time.perf_counter() - started) * 1000)
                query_counts.append(len(queries))
            max_elapsed = max(elapsed)
            max_query_count = max(query_counts)
            self.stdout.write(
                f"{name}: status={response.status_code} "
                f"avg_ms={sum(elapsed) / len(elapsed):.1f} "
                f"max_ms={max_elapsed:.1f} "
                f"avg_queries={sum(query_counts) / len(query_counts):.1f} "
                f"max_queries={max_query_count}"
            )
            if response.status_code != 200:
                raise CommandError(f"{name} returned HTTP {response.status_code}")
            if max_elapsed > max_ms:
                raise CommandError(
                    f"{name} exceeded latency budget: {max_elapsed:.1f} ms > {max_ms:.1f} ms"
                )
            if max_query_count > max_queries:
                raise CommandError(
                    f"{name} exceeded SQL budget: {max_query_count} > {max_queries} queries"
                )
