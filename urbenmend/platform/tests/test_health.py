"""
Health endpoint tests (A8, T0.8).

`GET /api/v1/health` is the K8s readiness probe target, so its status code has operational
consequences: a 503 pulls the pod out of the load balancer [doc: DevOps §8.4]. These assert the
required/optional split has teeth in both directions — a required failure must evict, and an
optional one must not.

⚠️ No `django_db` marker anywhere. The probes are exercised against patched checks rather than a
live connection: A7 is outstanding, so a real DB-backed request would error for an unrelated
reason. The connection itself is `check_database`'s one line of code and is covered by the
integration stage once migrations exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from rest_framework import status as http_status
from rest_framework.test import APIRequestFactory

from urbenmend.platform import views
from urbenmend.platform.selectors import DependencyHealth

factory = APIRequestFactory()


def call_health(dependencies: list[DependencyHealth]) -> Any:
    """Invoke the view with `check_all` patched to a fixed set of results."""
    with mock.patch.object(views, "check_all", return_value=dependencies):
        return views.health(factory.get("/api/v1/health"))


OK_DB = DependencyHealth(name="database", status="ok", required=True)
OK_CACHE = DependencyHealth(name="cache", status="ok", required=True)
DEAD_DB = DependencyHealth(
    name="database", status="unavailable", required=True, detail="Database is not reachable."
)
DEAD_CACHE = DependencyHealth(
    name="cache", status="unavailable", required=True, detail="Cache is not reachable."
)


def test_all_healthy_returns_200_and_ok() -> None:
    response = call_health([OK_DB, OK_CACHE])

    assert response.status_code == http_status.HTTP_200_OK
    assert response.data["status"] == "ok"
    assert response.data["dependencies"]["database"]["status"] == "ok"
    assert response.data["dependencies"]["cache"]["status"] == "ok"


def test_a_required_failure_returns_503() -> None:
    """503 is what evicts the pod from the load balancer (DevOps §8.4).

    A 200 with `"status": "unavailable"` in the body would leave K8s routing traffic to a pod
    that cannot serve it — the probe reads the status code, not the JSON.
    """
    response = call_health([DEAD_DB, OK_CACHE])

    assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.data["status"] == "unavailable"


def test_the_cache_is_required_because_sessions_live_in_it() -> None:
    """Redis is not merely a cache: `SESSION_ENGINE` is `cached_db`.

    Losing it breaks authentication for every request, and DevOps §8.4 uses Redis as its own
    example of a dependency whose loss marks the pod not-ready.
    """
    assert call_health([OK_DB, DEAD_CACHE]).status_code == (
        http_status.HTTP_503_SERVICE_UNAVAILABLE
    )


def test_an_optional_failure_degrades_but_stays_200() -> None:
    """⚠️ The direction that matters most.

    NFR-4 says an LLM outage degrades a feature; it must not take the deployment offline. If an
    optional dependency could force a 503, a triage outage would evict every pod in the fleet.
    """
    llm_down = DependencyHealth(
        name="llm", status="unavailable", required=False, detail="Triage provider unreachable."
    )

    response = call_health([OK_DB, OK_CACHE, llm_down])

    assert response.status_code == http_status.HTTP_200_OK
    assert response.data["status"] == "degraded"
    assert response.data["dependencies"]["llm"]["status"] == "unavailable"


def test_degraded_is_distinguishable_from_unavailable() -> None:
    """Three states, so a dashboard can tell "a feature is degraded" from "cannot serve".

    The flat per-dependency map alone leaves no room for that reading.
    """
    assert call_health([OK_DB, OK_CACHE]).data["status"] == "ok"
    assert (
        call_health(
            [OK_DB, OK_CACHE, DependencyHealth(name="llm", status="degraded", required=False)]
        ).data["status"]
        == "degraded"
    )
    assert call_health([DEAD_DB, OK_CACHE]).data["status"] == "unavailable"


def test_a_required_failure_outranks_an_optional_one() -> None:
    """Both failing must report `unavailable`, not `degraded` — the pod still cannot serve."""
    llm_down = DependencyHealth(name="llm", status="unavailable", required=False)
    assert call_health([DEAD_DB, llm_down]).data["status"] == "unavailable"


def test_detail_is_omitted_when_empty() -> None:
    """API §1.2 omits a field with nothing to say."""
    assert "detail" not in call_health([OK_DB, OK_CACHE]).data["dependencies"]["database"]


def test_failure_detail_never_leaks_the_topology() -> None:
    """⚠️ The endpoint is unauthenticated (API §6.16).

    A psycopg or redis error carries the host, database name and user from the DSN. The message
    is logged, but what goes on the wire is a fixed string — otherwise the probe hands an
    unauthenticated caller the deployment's connection details.
    """
    detail = call_health([DEAD_DB, OK_CACHE]).data["dependencies"]["database"]["detail"]

    assert detail == "Database is not reachable."
    for leak in ("password", "postgres://", "@", "5432", "urbenmend:"):
        assert leak not in detail


def test_the_probe_is_unauthenticated() -> None:
    """A probe whose credential expired would mark healthy pods not-ready.

    Also asserted structurally, because the project default is `IsAuthenticated` — a future
    refactor that drops the decorator would silently break every readiness probe in the cluster.
    """
    from rest_framework.permissions import AllowAny

    assert views.health.cls.permission_classes == [AllowAny]


def test_only_get_is_allowed() -> None:
    """A probe endpoint takes no writes. Asserted through the view, not its attributes."""
    response = views.health(factory.post("/api/v1/health"))
    assert response.status_code == http_status.HTTP_405_METHOD_NOT_ALLOWED


# ------------------------------------------------------------------------------------------
# Selector-level probes
# ------------------------------------------------------------------------------------------
@pytest.fixture
def _no_cache_backend() -> Iterator[None]:
    """Point the cache at an unreachable host so the probe's failure path runs for real."""
    from django.test import override_settings

    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": "redis://127.0.0.1:1/0",
                "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            }
        }
    ):
        yield


def test_a_probe_reports_failure_rather_than_raising(_no_cache_backend: None) -> None:
    """⚠️ A probe that propagates its exception yields a 500, not a 503.

    Kubernetes treats both as not-ready, but a 500 loses the per-dependency body that says which
    dependency failed — and the same view serves the human-facing status.
    """
    from django.core.cache import caches

    caches._settings_to_connect = {}  # type: ignore[attr-defined]
    from urbenmend.platform.selectors import check_cache

    result = check_cache()

    assert result.status == "unavailable"
    assert result.required is True


def test_check_all_reports_only_dependencies_that_exist() -> None:
    """⚠️ API §6.16 also asks for LLM/geocoder flags, and neither subsystem is built.

    A hard-coded `"llm": "ok"` would be a fabricated flag on the endpoint the readiness probe
    trusts — worse than an absent one. The provider is unpinned (Q9) and the geocoder is an
    unprovisioned adapter (ASSUMP-5); their probes land with those subsystems.
    """
    from urbenmend.platform.selectors import check_all

    with (
        mock.patch("urbenmend.platform.selectors.check_database", return_value=OK_DB),
        mock.patch("urbenmend.platform.selectors.check_cache", return_value=OK_CACHE),
    ):
        assert [dep.name for dep in check_all()] == ["database", "cache"]
