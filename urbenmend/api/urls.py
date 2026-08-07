"""
`/api/v1` URL surface (A8, T0.6).

Mounted at `api/v1/` by the root URLconf, so this module carries no version prefix of its own —
URI versioning lives in one place [doc: API §5].

⚠️ Every route added here must trace to an endpoint in `docs/04-api-specification.md`. That
spec is authoritative over the implementation; if code needs to differ, the spec is amended
first [doc: CLAUDE.md, API §1].
"""

from __future__ import annotations

from django.urls import path

from urbenmend.identity import views as identity_views
from urbenmend.platform import views as platform_views

app_name = "api"

urlpatterns = [
    # API §6.16 — liveness/readiness with dependency degradation flags. The K8s readiness
    # probe targets this path [doc: DevOps §8.4].
    path("health", platform_views.health, name="health"),
    # API §6.1 — authentication. T1.2 covers register + verify; T1.3 adds login + logout.
    # 2fa/password are T1.7 and are deliberately absent until then rather than stubbed.
    path("auth/register", identity_views.RegisterView.as_view(), name="auth-register"),
    path("auth/verify", identity_views.VerifyView.as_view(), name="auth-verify"),
    path("auth/login", identity_views.LoginView.as_view(), name="auth-login"),
    path("auth/logout", identity_views.LogoutView.as_view(), name="auth-logout"),
    # API §6.2 — users. T1.6 adds Authority provisioning (FR-2, BR-25).
    #
    # ⚠️ Registered before any `users/<id>` route, and it must stay that way. Django matches in
    # order, so a `users/<uuid:pk>` pattern added above this line would still not shadow it
    # (`authorities` is not a UUID) — but a looser `users/<str:pk>` from T1.9 would swallow it and
    # turn provisioning into a lookup for a user whose id is the literal string "authorities".
    path(
        "users/authorities",
        identity_views.ProvisionAuthorityView.as_view(),
        name="users-authorities",
    ),
]

# Remaining §6 resources land with their phases, not here:
#   /auth/2fa, /auth/password        → T1.7
#   /users/me, /users, /users/{id}   → T1.9
#   /reports, /media                 → P1/P3
#   /issues, /map, /comments         → P2/P4
#   /meta/enums                      → P1 (taxonomy confirmed — Q1 resolved 2026-08-07)
# ⚠️ No `POST /issues` ever: Issues form only via async clustering [doc: API §3, CLAUDE.md].
