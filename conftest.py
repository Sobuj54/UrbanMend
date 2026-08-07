"""
Project-wide pytest fixtures.

⚠️ **Created in T1.8 for one reason: throttle state is not rolled back.** `pytest-django` wraps
each test in a database transaction, so DB rows vanish between tests — but rate-limit counters live
in the Redis `default` cache, which nothing resets. Without `_reset_throttle_cache` below, buckets
accumulate across the whole session: unrelated tests written before T1.8 exhaust the per-IP window
partway through the run and fail with a spurious `429`, and *which* ones fail depends on test order.

This is autouse and session-wide on purpose. Making each new test remember to clear the cache is
the kind of rule that holds until someone adds a throttled endpoint and does not know it exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from django.core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_throttle_cache() -> Iterator[None]:
    """Clear the cache around every test — see the module docstring.

    ⚠️ Clears **before and after**. Before, so a test never inherits a partly-spent bucket; after,
    so a test that trips a limit deliberately (T1.8's own suite) does not leave the next one
    starting from a `429`.

    ⚠️ This clears the whole `default` cache, which is also the session cache
    (`SESSION_CACHE_ALIAS`). That is safe because sessions use `cached_db` — the DB row is the
    source of truth and a cleared cache is re-read from it, not lost. It would NOT be safe under a
    pure `cache` session backend; if that ever changes, this fixture must clear only throttle keys.
    """
    cache.clear()
    yield
    cache.clear()
