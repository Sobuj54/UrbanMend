"""
Reporting — HTTP layer (T2.2 submit, T2.7 read, T2.8 edit, T2.9 submission throttling).

Views stay thin (Arch §2.4, R-12/DC-3): parse, call the service or selector, render. Every rule
these endpoints enforce — Citizen-only, BR-2, BR-3, BR-35, the per-role visibility of the list, and
the author-pre-triage vs. official-re-categorize split on `PATCH` — lives in `reporting/services.py`
and `reporting/selectors.py`, which are the enforcement points (FR-3).

[doc: API §6.3; FR-5, FR-11, FR-33, NFR-2, NFR-3]
"""

from __future__ import annotations

from typing import Any, cast

from rest_framework import status
from rest_framework.exceptions import NotAuthenticated
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.api.throttling import (
    RateLimitHeadersMixin,
    SubmissionIPRateThrottle,
    SubmissionRateThrottle,
)
from urbenmend.identity.models import User
from urbenmend.reporting import selectors, services
from urbenmend.reporting.pagination import ReportCursorPagination
from urbenmend.reporting.serializers import (
    LocationSerializer,
    ReportDetailSerializer,
    ReportListQuerySerializer,
    ReportPatchSerializer,
    ReportSubmitResponseSerializer,
    ReportSubmitSerializer,
)


class ReportCollectionView(RateLimitHeadersMixin, APIView):
    """`POST /reports` and `GET /reports` — one path, two verbs (API §6.3).

    ⚠️ **One view, because §6.3 gives both operations the same URL.** Two `APIView` subclasses on
    one `path()` is not expressible in Django's router; splitting them would need a dispatching
    wrapper whose only job is to undo the split.

    ⚠️ **Both verbs require a session, but for different reasons, and only one is public-adjacent.**
    `GET /reports/{id}` is public (Q7) — the *collection* is not, because there is no such thing as
    "all reports" without a caller: §6.3 scopes it to own/in-scope/all by role. An anonymous list
    would have to mean "every report in the city", which is a different endpoint nobody specified.

    ⚠️ **No role check in `permission_classes`.** `IsAuthenticated` establishes *who*; the
    Citizen-only submit rule is `author.role != Role.CITIZEN` inside `create_report()` and the list's
    three-way visibility rule is in `list_reports()` (FR-3). A DRF permission class here would read
    as the enforcement point and drift from it — the reasoning `ProvisionAuthorityView` records.

    ⚠️ **`POST` is rate limited (T2.9, FR-33) and `GET` is not.** FR-33 is about submission; a read
    that costs one indexed query is not the abuse surface, and throttling it would break the map and
    the Authority queue for a legitimately busy operator. `get_throttles()` below is where that split
    is made, because `throttle_classes` alone applies to every method on the view.
    """

    permission_classes = [IsAuthenticated]
    # ⚠️ **Both buckets, and neither is redundant.** `SubmissionRateThrottle` caps one account;
    # `SubmissionIPRateThrottle` caps one source across many accounts, which is the Sybil attack
    # PRD §T3 names and which no per-account bucket can see. Whichever is tighter binds.
    throttle_classes = [SubmissionRateThrottle, SubmissionIPRateThrottle]

    def get_throttles(self) -> list[Any]:
        """Throttle `POST` only — FR-33 limits submission, not reading (API §4.5).

        ⚠️ **Returning `[]` for `GET` also suppresses the `RateLimit-*` headers on it, deliberately.**
        The mixin captures its instances inside `super().get_throttles()`, so an unthrottled method
        reports nothing — §4.5 requires the headers on "every limited endpoint", and advertising a
        limit `GET` does not enforce is the failure `test_an_unthrottled_endpoint_advertises_no_rate_limit_headers`
        exists to catch on `/health`.

        ⚠️ **`self.request`, not an argument.** DRF calls `get_throttles()` with no parameters from
        `check_throttles()`, and `dispatch()` has already assigned `self.request` by then. A method
        signature that took the request would simply never be called with one.
        """
        if self.request.method != "POST":
            return []
        return super().get_throttles()

    def post(self, request: Request) -> Response:
        """Persist now, triage later — `202` (API §6.3, FR-5, NFR-3).

        ⚠️ **`202`, not `201`.** The row is durable when this returns, but the resource is
        *incomplete*: category, severity signal and Issue link are all empty until the worker
        finishes (Arch §4). A `201` with `Location` would promise a settled resource, and §6.3
        fixes `202`.

        ⚠️ **Still no `Location` header, now that `GET /reports/{id}` exists.** §6.3 does not list
        one, and `Location` belongs to `201`: pointing a client at a URL that currently answers with
        an empty `classification` invites it to treat the read as final. §6.3 already tells clients
        to poll that route for current state.

        ⚠️ **T2.3: the `Idempotency-Key` header is read here and resolved in the service** (plan
        T2.3, §4.6). This layer does exactly two idempotency-related things — lift the header off the
        request, and turn the resulting `replayed` flag into the `Idempotency-Replayed` response
        header. Both are HTTP-shaped facts. Everything else (scope, fingerprint, reservation,
        replay) is a rule, so it lives in `submit_report()` where a non-HTTP caller gets it too.
        """
        serializer = ReportSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ⚠️ `cast`, not a guard: `IsAuthenticated` has already run. The service re-reads `role`
        # off whatever it is handed, so nothing downstream trusts this annotation — same posture as
        # `ProvisionAuthorityView`, and `test_unauthenticated_gets_401` keeps it honest.
        author = cast("User", request.user)

        # ⚠️ Not built inline from `data["location"]` — the `(lng, lat)` argument order is the trap
        # (transposing it silently rejects every real submission), so it lives in exactly one
        # place. `LocationSerializer` owns it; this line consumes it.
        point = LocationSerializer.to_point(data["location"])

        acknowledgement = services.submit_report(
            author=author,
            location=point,
            description=data["description"],
            category_slug=data.get("category"),
            # FR-12 — fall back to the citizen's own preference, not the column default `"en"`.
            # A Bangla-preferring citizen whose client omits the field should not have their
            # report classified and notified about in English.
            language=data.get("language") or author.preferred_language,
            # ⚠️ The ids are handed over **unresolved**: the serializer proved they are UUIDs, and
            # `media.services.resolve_media_for_attachment()` owns ownership, single-use and the
            # per-report cap (FR-3). Counting or filtering them here would put half of BR-3's input
            # in the view. `.get(..., [])` because the field is `required=False` — absent and empty
            # are the same submission.
            media_ids=data.get("media_ids", []),
            # ⚠️ `request.headers` is case-insensitive, so `idempotency-key` from a lowercasing
            # HTTP/2 client resolves the same as the spelling §6.3 documents. `None` when absent —
            # the service treats that, and a blank value, as "no de-duplication requested" (§4.6).
            idempotency_key=request.headers.get("Idempotency-Key"),
        )

        # ⚠️ Nothing here catches `ReportValidationError`, `OutOfCity`, `IdempotencyKeyReused` or
        # `IdempotencyInProgress`. `urbenmend_exception_handler` already renders the first as
        # `400 VALIDATION_FAILED` (it subclasses Django's `ValidationError`), the second as
        # `422 OUT_OF_CITY`, and the two idempotency conflicts as `409` with their own codes (they
        # are DRF `APIException`s carrying those `default_code`s). A local `except` would only be a
        # chance to get the status wrong, and would collapse the distinctions those types exist for.
        response = Response(
            ReportSubmitResponseSerializer(acknowledgement).data,
            status=status.HTTP_202_ACCEPTED,
        )
        if acknowledgement.replayed:
            # ⚠️ §4.6: "the only way a client — or an operator reading a log — can tell a
            # re-delivered acknowledgement from a first one, since by design the bodies are
            # identical". Set only on a replay: an always-present `false` would be one more thing a
            # client could branch on, and §4.6 describes the header as additive.
            response["Idempotency-Replayed"] = "true"
        return response

    def get(self, request: Request) -> Response:
        """The caller's reports, filtered and cursor-paginated — `200` (API §6.3, §1.3, §4.4).

        ⚠️ **The query serializer runs before the selector, and its `400`s are the point.** Reading
        `request.query_params.get()` here instead would ignore `?statuss=triaged` and answer `200`
        with the *unfiltered* list — a citizen looking for their open reports would be shown all of
        them with no signal that the filter was dropped (api-conventions.md fixes `400` for an
        unknown param).

        ⚠️ **The paginator is instantiated here rather than taken from `DEFAULT_PAGINATION_CLASS`.**
        `ReportCursorPagination` needs the validated sort direction to build a correct cursor
        tie-break, and `APIView` (unlike `GenericAPIView`) has no `self.paginator` to configure. One
        explicit construction beats a class attribute plus a hook that mutates it.
        """
        params = ReportListQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        filters = params.validated_data

        actor = cast("User", request.user)

        queryset = selectors.list_reports(
            actor=actor,
            # `validate_status`/`validate_category` return lists; absent means "no filter", which is
            # `()` rather than `None` so the selector never has to distinguish the two.
            statuses=filters.get("status", ()),
            category_slugs=filters.get("category", ()),
            query=filters.get("q", ""),
            # ⚠️ `to_point()` on the serializer, not a `Point(...)` built here — the `(lng, lat)`
            # order lives in `LocationSerializer` and nowhere else. `validate()` has already
            # guaranteed that a centre and a radius arrive together or not at all.
            near=params.to_point(),
            radius_m=filters.get("radius_m"),
        )

        paginator = ReportCursorPagination(ascending=params.ascending)
        page = paginator.paginate_queryset(queryset, request, view=self)
        # ⚠️ DRF types this `list | None` because `CursorPagination` returns `None` when paging is
        # disabled, which cannot happen here (`page_size` is 20 and NFR-2 makes paging mandatory).
        # `or []` rather than falling back to the unpaginated queryset: if that invariant ever broke,
        # an empty page is a visible bug, while silently streaming every report in the city is the
        # kind that only surfaces under load.
        rows = page or []
        return paginator.get_paginated_response(ReportDetailSerializer(rows, many=True).data)


class ReportDetailView(APIView):
    """`GET /reports/{id}` (public) and `PATCH /reports/{id}` (author or official) — API §6.3.

    ⚠️ **`AllowAny`, because §6.3 marks the read `Auth: none (public)`** (Q7 resolved report
    visibility as public, and `GET /media/{id}` follows it). The project default is
    `IsAuthenticated`, so this override is what makes the endpoint reachable at all — and it is why
    `patch()` raises `NotAuthenticated` itself rather than relying on a permission class.

    ⚠️ **`authentication_classes` is NOT emptied.** Setting it to `[]` is what silently disables CSRF
    (the T1.3 trap `identity/tests/test_csrf.py` exists to catch), and this class carries a mutating
    `PATCH`. `AllowAny` skips the *permission* check while `SessionAuthentication` still runs, so an
    authenticated write here stays CSRF-protected and an anonymous read still works.

    ⚠️ **The `404`/`410` split is the selector's, not this view's.** `get_report_for_read()` raises
    both, and **both verbs go through it** — so a management command and a future GeoJSON map
    endpoint get the same disclosure behaviour; a local `try/except` here would be a second place for
    it to be decided.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request, report_id: str) -> Response:
        """`200` with the Report resource. `404` when absent, `410` when moderated."""
        report = selectors.get_report_for_read(report_id=report_id)
        return Response(ReportDetailSerializer(report).data, status=status.HTTP_200_OK)

    def patch(self, request: Request, report_id: str) -> Response:
        """The pre-triage edit and the human re-categorization — `200` (API §6.3, FR-11, T2.8).

        ⚠️ **An anonymous `PATCH` must be `401`, not `403`.** `AllowAny` is on this class so the
        public `GET` is reachable, which means `request.user` can be `AnonymousUser` here — so the
        method checks authentication itself and raises DRF's `NotAuthenticated`, which the handler
        rewrites back to `401` globally (§4.2). Passing an `AnonymousUser` to `update_report()` would
        instead read `.role` off a model that has none, i.e. a `500`. `MediaDetailView.delete()`
        records the identical decision.

        ⚠️ **A per-method `permission_classes` was not used to express this.** DRF resolves
        permissions once per request, before dispatch, so a method-scoped override needs
        `get_permissions()` branching on `request.method` — half the authorization moved into the
        view, which is exactly what FR-3 puts in the service layer. One explicit guard on the one
        mutating method is smaller and reads as what it is.

        ⚠️ **The selector is reused, so a `PATCH` against a moderated report answers `410`** rather
        than `404` or a `403` that would confirm the row is still there. The author of a hidden
        report is told the content is gone — the same answer `GET` gives them — and the moderation
        review surface is §6.13's, not this one's.

        ⚠️ **Nothing here catches the service's exceptions.** `Conflict` carries §6.3's
        `NOT_EDITABLE`, `AuthorizationError`/`PermissionDenied` render as `403 FORBIDDEN` and
        `ReportValidationError` as `400 VALIDATION_FAILED` — `urbenmend_exception_handler` already
        maps all four. A local `except` would only be a chance to collapse the distinctions they
        exist for.
        """
        if not request.user or not request.user.is_authenticated:
            raise NotAuthenticated

        serializer = ReportPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        report = selectors.get_report_for_read(report_id=report_id)

        updated = services.update_report(
            actor=request.user,
            report=report,
            # ⚠️ `.get()` with no default, so an omitted field arrives as `None` — which is how
            # `update_report()` tells "not sent" from `""`. A `data.get("description", "")` here would
            # turn every category-only edit into a description blanking, and BR-3 would then reject a
            # perfectly good re-categorization of a photo-less report.
            description=data.get("description"),
            category_slug=data.get("category"),
        )
        return Response(ReportDetailSerializer(updated).data, status=status.HTTP_200_OK)
