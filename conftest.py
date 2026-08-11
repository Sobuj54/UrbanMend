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
from django.test import override_settings

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session", autouse=True)
def _in_memory_media_storage() -> Iterator[None]:
    """Swap object storage for `InMemoryStorage` for the whole run (T2.4).

    ⚠️ **Added in T2.4 for the same class of reason as `_reset_throttle_cache`: the default
    storage is not part of the test transaction.** `STORAGES["default"]` is `S3Boto3Storage`, so
    the moment a `Media` fixture existed, saving one opened a real connection to the MinIO
    container — and with no `STORAGE_ACCESS_KEY` in the test environment botocore fell through to
    an EC2 instance-metadata lookup and failed with `NoCredentialsError` after a timeout.

    ⚠️ **A real `Storage`, not a mock.** `InMemoryStorage` implements `save`, `open`, `exists`,
    `delete` and `url`, so `upload_media()`'s `media.file.save(...)` and the serializer's
    `stored.url` run their genuine code paths — only the network is gone. A patched-out storage
    would let a regression that never writes the sanitized bytes pass unnoticed.

    ⚠️ **Overridden here rather than in `settings/dev.py`.** `dev.py` is what the local dev server
    runs on, where uploads are meant to reach MinIO; flipping it there would make the running
    application silently lose every photo on restart.

    ⚠️ `staticfiles` is respecified because `override_settings` replaces `STORAGES` wholesale
    rather than merging into it — omitting the key leaves `staticfiles_storage` unresolvable.
    """
    with override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    ):
        yield


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
