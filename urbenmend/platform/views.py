"""
System endpoints (A8, T0.8).

`GET /api/v1/health` — the K8s readiness probe target [doc: DevOps §8.4, API §6.16].

⚠️ Unauthenticated by design: a probe whose credential expired would mark healthy pods
not-ready. The response deliberately omits connection strings, hosts and internal names so the
endpoint can stay public without leaking the topology.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from urbenmend.platform.selectors import check_all


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request: Request) -> Response:
    """Dependency degradation flags for the readiness probe.

    200 when every required dependency is reachable; 503 when any required one fails. A 503
    pulls the pod out of the load balancer [doc: DevOps §8.4], so the required/optional split
    in `platform/selectors.py` has teeth — marking an optional dependency required would take
    the deployment offline for a failure NFR-4 says must only degrade a feature.

    Optional dependencies report their status but never force a 503. The LLM and geocoder
    probes will land here when those subsystems exist (P2/P3); until then only the database
    and cache are checked.
    """
    results = check_all()

    required_failed = any(dep.required and dep.status != "ok" for dep in results)
    optional_failed = any(not dep.required and dep.status != "ok" for dep in results)

    dependencies: dict[str, dict[str, str]] = {}
    for dep in results:
        entry: dict[str, str] = {"status": dep.status}
        # `detail` omitted when empty — API §1.2 allows omitting a field with nothing to say.
        if dep.detail:
            entry["detail"] = dep.detail
        dependencies[dep.name] = entry

    # Three-state overall verdict, so a dashboard can distinguish "a feature is degraded"
    # (NFR-4 working as designed) from "this pod cannot serve" — the flat per-dependency map
    # alone leaves no room for that reading.
    if required_failed:
        overall_status = "unavailable"
    elif optional_failed:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return Response(
        {"status": overall_status, "dependencies": dependencies},
        # Only a *required* failure returns 503. A degraded optional dependency stays 200 or
        # the readiness probe would evict a pod that is still fully able to serve.
        status=(status.HTTP_503_SERVICE_UNAVAILABLE if required_failed else status.HTTP_200_OK),
    )
