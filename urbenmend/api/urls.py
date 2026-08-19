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

from urbenmend.export import views as export_views
from urbenmend.audit import views as audit_views
from urbenmend.moderation import views as moderation_views
from urbenmend.classification import views as classification_views
from urbenmend.geo import views as geo_views
from urbenmend.identity import views as identity_views
from urbenmend.issues import views as issue_views
from urbenmend.media import views as media_views
from urbenmend.notifications import views as notification_views
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
    # API §6.3 — reports. T2.2 adds submission (`POST`), T2.7 the collection read (`GET`) and the
    # detail read below; T2.8 adds the edit (`PATCH`) to the same detail route.
    #
    # ⚠️ **Registered before the `reports/<id>` route below**, for the third time in this file:
    # `<uuid:…>` is narrow enough that it could not shadow the bare collection path today, but the
    # ordering habit is what keeps a later `reports/mine`-style addition safe. Order in this file IS
    # the router.
    #
    # ⚠️ **One view for both verbs, and the name stays `reports`.** §6.3 gives `POST` and `GET` the
    # same URL, so `ReportCollectionView` carries both; two classes on one `path()` is not
    # expressible. The route *name* is unchanged from T2.2 on purpose — `reverse("api:reports")` is
    # already used by the submission tests and by clients' generated code.
    path("reports", reporting_views.ReportCollectionView.as_view(), name="reports"),
    # ⚠️ `<uuid:report_id>`, not `<str:…>`: the converter refuses a non-UUID at the routing layer, so
    # a scan for `/reports/1` answers `404` without reaching a view — and no selector has to defend
    # against a `ValueError` from the ORM to avoid a `500`. Same decision as `media/<uuid:media_id>`.
    path(
        "reports/<uuid:report_id>",
        reporting_views.ReportDetailView.as_view(),
        name="reports-detail",
    ),
    # API §6.5 — the authority work queue. `GET` only: Issues form via async clustering, never a
    # client POST (the note at the foot of this file).
    #
    # ⚠️ **Collection before the detail patterns**, for the third time in this file. `<uuid:…>` is
    # narrow enough that it could not shadow the bare `issues` path today, but T7.3's
    # `issues/<uuid:issue_id>` will sit in this same block, and the ordering habit is what keeps that
    # addition safe. Order in this file IS the router.
    path("issues", issue_views.IssueCollectionView.as_view(), name="issues"),
    # API §6.6 — nested-only, revocable "me-too" confirmations. The collection accepts POST;
    # `/me` accepts DELETE, so separate views prevent either verb from leaking onto the other path.
    path(
        "issues/<uuid:issue_id>/status",
        issue_views.IssueStatusView.as_view(),
        name="issues-status",
    ),
    path(
        "issues/<uuid:issue_id>/status-events",
        issue_views.IssueStatusEventsView.as_view(),
        name="issues-status-events",
    ),
    path(
        "issues/<uuid:issue_id>/assignment",
        issue_views.IssueAssignmentView.as_view(),
        name="issues-assignment",
    ),
    path(
        "issues/<uuid:issue_id>/severity",
        issue_views.IssueSeverityView.as_view(),
        name="issues-severity",
    ),
    path(
        "issues/<uuid:issue_id>/merge",
        issue_views.IssueMergeView.as_view(),
        name="issues-merge",
    ),
    path(
        "issues/<uuid:issue_id>/split",
        issue_views.IssueSplitView.as_view(),
        name="issues-split",
    ),
    path(
        "issues/<uuid:issue_id>/confirmations",
        issue_views.IssueConfirmationCreateView.as_view(),
        name="issues-confirmations",
    ),
    path(
        "issues/<uuid:issue_id>/confirmations/me",
        issue_views.IssueConfirmationDeleteView.as_view(),
        name="issues-confirmations-me",
    ),
    path(
        "issues/<uuid:issue_id>/comments",
        issue_views.IssueCommentsView.as_view(),
        name="issues-comments",
    ),
    path(
        "issues/<uuid:issue_id>/comments/<uuid:comment_id>",
        issue_views.IssueCommentDetailView.as_view(),
        name="issues-comment-detail",
    ),
    path("issues/<uuid:issue_id>", issue_views.IssueDetailView.as_view(), name="issues-detail"),
    path(
        "issues/<uuid:issue_id>/reports",
        issue_views.IssueReportsView.as_view(),
        name="issues-reports",
    ),
    path("map/issues", issue_views.IssueMapView.as_view(), name="map-issues"),
    path("analytics/summary", issue_views.AnalyticsSummaryView.as_view(), name="analytics-summary"),
    path("audit-events", audit_views.AuditEventCollectionView.as_view(), name="audit-events"),
    path("categories", classification_views.CategoryCollectionView.as_view(), name="categories"),
    path("categories/<slug:key>", classification_views.CategoryDetailView.as_view(), name="categories-detail"),
    path("severity-keywords", classification_views.SeverityKeywordCollectionView.as_view(), name="severity-keywords"),
    path("severity-keywords/<int:keyword_id>", classification_views.SeverityKeywordDetailView.as_view(), name="severity-keywords-detail"),
    path("clustering-rules", issue_views.ClusteringRuleCollectionView.as_view(), name="clustering-rules"),
    path("clustering-rules/<int:rule_id>", issue_views.ClusteringRuleDetailView.as_view(), name="clustering-rules-detail"),
    path("pois", geo_views.POICollectionView.as_view(), name="pois"),
    path("pois/<uuid:poi_id>", geo_views.POIDetailView.as_view(), name="pois-detail"),
    path("meta/city-boundary", geo_views.CityBoundaryView.as_view(), name="city-boundary"),
    path("reports/<uuid:pk>/moderation", moderation_views.ReportModerationView.as_view(), name="reports-moderation"),
    path("issues/<uuid:pk>/moderation", moderation_views.IssueModerationView.as_view(), name="issues-moderation"),
    path("media/<uuid:pk>/moderation", moderation_views.MediaModerationView.as_view(), name="media-moderation"),
    path("issues/<uuid:pk>/comments/<uuid:comment_id>/moderation", moderation_views.CommentModerationView.as_view(), name="comments-moderation"),
    path("exports", export_views.ExportCollectionView.as_view(), name="exports"),
    path(
        "exports/<uuid:export_id>", export_views.ExportDetailView.as_view(), name="exports-detail"
    ),
    # API §6.4 — media. T2.4 adds upload + read + moderation-remove; T2.5 is the worker that
    # builds the derivatives, so a fresh upload answers `state: "processing"` and a null
    # `thumbnailUrl` until it runs.
    #
    # ⚠️ **Collection before the detail pattern**, for the fourth time in this file. `<uuid:…>` is
    # narrow enough that it could not shadow the bare path today, but the ordering habit is what
    # keeps a later `media/batch`-style addition safe. Order in this file IS the router.
    path("media", media_views.MediaUploadView.as_view(), name="media"),
    # ⚠️ `<uuid:media_id>`, not `<str:…>`: the converter refuses a non-UUID at the routing layer,
    # so a scan for `/media/1` answers `404` without reaching a view — and no selector has to
    # defend against a `ValueError` from the ORM to avoid a `500`.
    path("media/<uuid:media_id>", media_views.MediaDetailView.as_view(), name="media-detail"),
    # API section 6.11 - self-owned in-app notification reads and read-state mutations.
    path(
        "notifications/read-all",
        notification_views.NotificationReadAllView.as_view(),
        name="notifications-read-all",
    ),
    path(
        "notifications/<uuid:notification_id>",
        notification_views.NotificationDetailView.as_view(),
        name="notifications-detail",
    ),
    path(
        "notifications",
        notification_views.NotificationCollectionView.as_view(),
        name="notifications",
    ),
    path(
        "notification-preferences",
        notification_views.NotificationPreferenceView.as_view(),
        name="notification-preferences",
    ),
]

# Remaining §6 resources land with their phases, not here:
#   /auth/password                   → unowned; ❓Q5 blocks delivery of the reset token
#   /users/me                        → T1.9 ✅ built
#   /users, /users/{id}              → unowned (admin list/search, role/scope/status change);
#                                      API §6.2 names both, T1.9 scoped them out
#   POST /reports                    → T2.2 ✅ built; T2.9 ✅ throttled (per-account + per-IP)
#   GET /reports, GET /reports/{id}  → T2.7 ✅ built (list is session-scoped by role; detail public)
#   PATCH /reports/{id}              → T2.8 ✅ built (author pre-triage; Authority/Admin
#                                      re-categorize only — FR-11)
#   /media                           → T2.4 ✅ built; T2.5 ✅ derivatives (❓Q6 resolved: strip all
#                                      EXIF always); T2.9 ✅ POST throttled. ⚠️ The reads and the
#                                      moderation DELETE stay unthrottled — FR-33 is about
#                                      submission, and both are covered by role checks instead
#   /issues/{id}/confirmations       → T4.7 ✅ built
#   PATCH /issues/{id}/status        → T5.2 ✅ built; T5.3 adds immutable event emission
#   PATCH /issues/{id}/assignment    → T5.4 ✅ built (Authority self-assign; Admin any)
#   PATCH /issues/{id}/severity      → T5.5 ✅ built (computed severity retained)
#   POST /issues/{id}/merge          → T5.6 ✅ built (path Issue survives)
#   POST /issues/{id}/split          → T5.7 ✅ built (selected Reports form new Issue)
#   GET /issues                      → T7.1/T7.2 ✅ built (public; Authority sees in-scope only per
#                                      BR-26, so a signed-in Authority sees *fewer* Issues than an
#                                      anonymous visitor — `list_issues()` records why)
#   GET /issues/{id}, /{id}/reports  → T7.3 (detail includes public comments, which need T5.8)
#   /map, /comments                  → later P4/P5 tasks
#   /meta/enums                      → P1 (taxonomy confirmed — Q1 resolved 2026-08-07)
# ⚠️ No `POST /issues` ever: Issues form only via async clustering [doc: API §3, CLAUDE.md].
