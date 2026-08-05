"""
Platform (cross-cutting) — read operations.

Query functions for this module. Kept separate from services.py so reads never acquire
write-path side effects, and so the modules that consume this one have a single documented
surface to call [doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

Currently holds the dependency probes behind `GET /api/v1/health` (A8, T0.8).

⚠️ **Which dependencies count as required has teeth.** The K8s readiness probe uses that
endpoint, and "a degraded dependency (e.g. Redis down) marks the pod not-ready and stops
traffic routing to it" [doc: DevOps §8.4]. Marking an *optional* dependency required would
take the deployment out of service for a failure NFR-4 says must only degrade a feature. The
split below follows the Arch §12 failure table.

[doc: Arch §3 (NFR-5, NFR-9, NFR-13), API §6.16]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from django.core.cache import cache
from django.db import connections

logger = structlog.get_logger(__name__)

Status = Literal["ok", "degraded", "unavailable"]

# Namespaced so the probe cannot collide with a real cache entry.
_CACHE_PROBE_KEY = "urbenmend:health:probe"


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    """One dependency's probe result.

    `required` decides whether a failure makes the endpoint 503 — i.e. whether it pulls the pod
    out of the load balancer.
    """

    name: str
    status: Status
    required: bool
    detail: str | None = None


def check_database() -> DependencyHealth:
    """Required — without Postgres no request can be served.

    `SELECT 1` on the default connection: cheap, and it exercises the real connection pool
    rather than merely asserting that settings parsed.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 — a probe reports every failure, never propagates.
        # ⚠️ The exception text is logged but NOT returned: a driver error can carry the host,
        # database name and user from the DSN, and /health is unauthenticated (API §6.16).
        logger.warning("health_check_failed", dependency="database", error=str(exc))
        return DependencyHealth(
            name="database",
            status="unavailable",
            required=True,
            detail="Database is not reachable.",
        )
    return DependencyHealth(name="database", status="ok", required=True)


def check_cache() -> DependencyHealth:
    """Required — because sessions live in it.

    Redis is not merely a cache here: `SESSION_ENGINE` is `cached_db` and rate limiting shares
    the store, so losing Redis breaks authentication for every request. DevOps §8.4 uses Redis
    as its own example of a dependency whose loss marks the pod not-ready.

    A write-then-read round-trip rather than `ping()`: a Redis that accepts connections but
    rejects writes (out of memory, replica misconfigured as primary) answers `ping` happily.
    """
    try:
        cache.set(_CACHE_PROBE_KEY, "1", timeout=10)
        if cache.get(_CACHE_PROBE_KEY) != "1":
            raise RuntimeError("cache round-trip returned an unexpected value")
    except Exception as exc:  # noqa: BLE001 — see check_database.
        logger.warning("health_check_failed", dependency="cache", error=str(exc))
        return DependencyHealth(
            name="cache",
            status="unavailable",
            required=True,
            detail="Cache/session store is not reachable.",
        )
    return DependencyHealth(name="cache", status="ok", required=True)


def check_all() -> list[DependencyHealth]:
    """Every probe, in a stable order.

    ⚠️ Deliberately short. API §6.16 asks for "LLM/geocoder up/fallback" flags and neither
    subsystem exists yet — the LLM provider is unpinned (Q9) and the geocoder is an
    unprovisioned adapter (ASSUMP-5). A probe reporting `"llm": "ok"` before either is built
    would be a fabricated flag on the endpoint the readiness probe trusts. Classification adds
    its probe in P2; the object store adds one when media upload lands (P3).
    """
    return [check_database(), check_cache()]
