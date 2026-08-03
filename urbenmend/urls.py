"""
Root URLconf (A4, T0.3) — deliberately close to empty.

`manage.py check` (and therefore the `collectstatic` step in the Dockerfile) imports
ROOT_URLCONF, so this module has to exist before anything else can run. The actual
`/api/v1` router is A8 / T0.6, and every route must trace to `docs/04-api-specification.md`.

⚠️ Do not add endpoints here ahead of the spec. `/api/v1/health` (API §6.16) is the K8s
readiness probe target [doc: DevOps §8.2] and lands with the rest of T0.6.
"""

from django.urls import URLPattern, URLResolver

# Annotated because the list is empty — mypy cannot infer an element type otherwise.
urlpatterns: list[URLPattern | URLResolver] = []

# A8 / T0.6 will add:
#   path("api/v1/", include("urbenmend.api_urls")),
#   path("admin/", admin.site.urls),         # FR-30/31, reference data + moderation
# ⚠️ /metrics is served by django_prometheus but must NOT be exposed on the Ingress
# [doc: DevOps §8.2] — mount it on a separate port or restrict it at the proxy.
