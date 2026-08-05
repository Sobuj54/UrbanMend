"""
Root URLconf (A4, T0.3; routes added in A8, T0.6).

URI versioning lives here — `api/v1/` is applied once at this level so no individual route
repeats it [doc: API §5].

⚠️ Do not add endpoints here ahead of the spec. Every route must trace to
`docs/04-api-specification.md`, which is authoritative over the implementation.
"""

from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

# Annotated because mypy cannot infer the element type from a heterogeneous list.
urlpatterns: list[URLPattern | URLResolver] = [
    path("api/v1/", include("urbenmend.api.urls")),
    # FR-30/FR-31 — reference data and moderation are surfaced through Django admin
    # [doc: Arch §2.4]. This is also the only consumer of `PermissionsMixin` on the user model.
    path("admin/", admin.site.urls),
    # django_prometheus' own URLconf, which serves /metrics.
    # ⚠️ Included so a pod-port scrape works [doc: DevOps §8.2], but it MUST NOT be reachable
    # through the Ingress — exposing it publishes the operational picture of the deployment.
    # Blocking it is an Ingress/proxy concern (§6.4); Django cannot enforce it here.
    path("", include("django_prometheus.urls")),
]
