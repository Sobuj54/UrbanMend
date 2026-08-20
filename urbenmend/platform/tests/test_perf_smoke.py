from io import StringIO

import pytest
from django.core import management
from django.core.management.base import CommandError


def test_perf_smoke_rejects_non_positive_latency_budget() -> None:
    with pytest.raises(CommandError, match="--max-ms must be positive"):
        management.call_command("perf_smoke", max_ms=0, stdout=StringIO())


def test_perf_smoke_rejects_zero_query_budget() -> None:
    with pytest.raises(CommandError, match="--max-queries must be at least 1"):
        management.call_command("perf_smoke", max_queries=0, stdout=StringIO())
