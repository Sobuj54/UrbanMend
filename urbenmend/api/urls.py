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
from urbenmend.reporting import views as reporting_views

app_name = "api"

urlpatterns = [
    # API §6.16 — liveness/readiness with dependency degradation flags. The K8s readiness
    # probe targets this path [doc: DevOps §8.4].
    path("health", platform_views.health, name="health"),
    # API §6.1 — authentication. T1.2 covers register + verify; T1.3 adds login + logout;
    # T1.7 adds the two 2FA routes. `/auth/password` remains deliberately absent rather than
    # stubbed — it is unbuilt and blocked on ❓Q5 for delivery of the reset token.
    path("auth/register", identity_views.RegisterView.as_view(), name="auth-register"),
    path("auth/verify", identity_views.VerifyView.as_view(), name="auth-verify"),
    path("auth/login", identity_views.LoginView.as_view(), name="auth-login"),
    # ⚠️ Both accept a *partial* post-password session, which is not an authenticated request —
    # see the T1.7 header in `identity/services.py`. They are the only two routes in the project
    # that do; anything else added under `auth/2fa/` needs that decision made deliberately.
    path(
        "auth/2fa/enroll",
        identity_views.TwoFactorEnrollView.as_view(),
        name="auth-2fa-enroll",
    ),
    path(
        "auth/2fa/verify",
        identity_views.TwoFactorVerifyView.as_view(),
        name="auth-2fa-verify",
    ),
    path("auth/logout", identity_views.LogoutView.as_view(), name="auth-logout"),
    # API §6.2 — users. T1.6 adds Authority provisioning (FR-2, BR-25).
    #
    # ⚠️ Registered before any `users/<id>` route, and it must stay that way. Django matches in
    # order, so a `users/<uuid:pk>` pattern added above this line would still not shadow it
    # (`authorities` is not a UUID) — but a looser `users/<str:pk>` for the still-unowned
    # `PATCH /users/{id}` would swallow it and turn provisioning into a lookup for a user whose id
    # is the literal string "authorities".
    path(
        "users/authorities",
        identity_views.ProvisionAuthorityView.as_view(),
        name="users-authorities",
    ),
    # API §6.2 — `/users/me` (T1.9): profile read/update and account deletion→anonymization.
    #
    # ⚠️ This route must stay registered before any `users/<id>` pattern for the same reason the
    # `authorities` route above must: a future `users/<str:pk>` from `PATCH /users/{id}` would
    # swallow `me` and turn profile reads into lookups for a user whose id is "me". Order in this
    # file IS the router.
    path("users/me", identity_views.MeView.as_view(), name="users-me"),
    # API §6.3 — reports. T2.2 adds submission only (`POST`); the reads are T2.7 and the
    # pre-triage edit is T2.8, so this path answers `405` to `GET` today rather than pretending.
    #
    # ⚠️ **Registered before any `reports/<id>` route must be**, for the third time in this file:
    # a later `reports/<str:pk>` cannot shadow the bare collection path, but the ordering habit is
    # what keeps `reports/me`-style additions safe. Order in this file IS the router.
    path("reports", reporting_views.ReportSubmitView.as_view(), name="reports"),
]

# Remaining §6 resources land with their phases, not here:
#   /auth/password                   → unowned; ❓Q5 blocks delivery of the reset token
#   /users/me                        → T1.9 ✅ built
#   /users, /users/{id}              → unowned (admin list/search, role/scope/status change);
#                                      API §6.2 names both, T1.9 scoped them out
#   POST /reports                    → T2.2 ✅ built (⚠️ unthrottled — FR-33 limits are T2.9)
#   GET /reports, GET /reports/{id}  → T2.7;  PATCH /reports/{id} → T2.8
#   /media                           → T2.4/T2.5 (❓Q6 gates the EXIF default)
#   /issues, /map, /comments         → P2/P4
#   /meta/enums                      → P1 (taxonomy confirmed — Q1 resolved 2026-08-07)
# ⚠️ No `POST /issues` ever: Issues form only via async clustering [doc: API §3, CLAUDE.md].
