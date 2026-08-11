"""
Media — HTTP layer (T2.4, T2.9 upload throttling).

Views stay thin (Arch §2.4, R-12/DC-3): parse, call the service, render. Every rule these endpoints
enforce — Citizen-only upload, the size/format/decode rejections, author-pre-triage vs. Admin
moderation on delete — lives in `media/services.py`, which is the enforcement point (FR-3).

[doc: API §6.4; FR-7, FR-31, FR-33, P3]
"""

from __future__ import annotations

from typing import cast

from rest_framework import status
from rest_framework.exceptions import NotAuthenticated
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.api.throttling import (
    MediaUploadRateThrottle,
    RateLimitHeadersMixin,
    SubmissionIPRateThrottle,
)
from urbenmend.identity.models import User
from urbenmend.media import selectors, services
from urbenmend.media.serializers import MediaResponseSerializer, MediaUploadSerializer


class MediaUploadView(RateLimitHeadersMixin, APIView):
    """`POST /media` — accept a photo, hand back a handle (API §6.4, FR-7).

    ⚠️ **`202`, not `201`.** §6.4 fixes it, and it is honest: the row is durable and the bytes are
    already EXIF-free when this returns, but the derivatives are not built yet, so the resource is
    incomplete. A `201` with `Location` would promise a settled resource.

    ⚠️ **`MultiPartParser` is declared on the view rather than relying on the project default.**
    `DEFAULT_PARSER_CLASSES` lists `JSONParser` first, and DRF picks by `Content-Type` — which works
    today, but a project-level parser change would silently turn every upload into a `415` from
    DRF's own machinery, with a code that is not §6.4's. Naming the two parsers this endpoint
    actually accepts keeps that decision local.

    ⚠️ **No role class in `permission_classes`.** `IsAuthenticated` establishes *who*; §6.4's
    Citizen-only rule is `actor.role != Role.CITIZEN` inside `upload_media()` (FR-3). A DRF
    permission class here would read as the enforcement point and drift from it — the reasoning
    `ReportCollectionView` and `ProvisionAuthorityView` both record.

    ⚠️ **Throttled by T2.9 (FR-33), and this is the more attractive target of the two submission
    routes** — each request costs a decode, a re-encode, a storage write and a worker job, where a
    report costs one INSERT. `MediaUploadRateThrottle` is therefore sized for what an upload costs
    rather than derived from the report limit; see `SUBMISSION_THROTTLE_RATES` in `settings/base.py`.

    ⚠️ **The bucket is consumed before the size and format checks run.** DRF calls
    `allow_request()` in `initial()`, so a rejected `413`/`415`/`422` still spends budget — which is
    the point: a script pushing 10 MiB of garbage costs more to serve than a real photo, not less.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    # ⚠️ **No `get_throttles()` split here, unlike `ReportCollectionView`** — this view carries only
    # `POST`, so `throttle_classes` already means "the upload". A `GET` added later would inherit the
    # submission buckets silently, which is why the reads live on `MediaDetailView` instead.
    #
    # ⚠️ `SubmissionIPRateThrottle` is the *same* bucket `POST /reports` uses, on purpose: five
    # photos and their report spend six units of one per-source allowance (PRD §T3).
    throttle_classes = [MediaUploadRateThrottle, SubmissionIPRateThrottle]

    def post(self, request: Request) -> Response:
        serializer = MediaUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # ⚠️ `cast`, not a guard: `IsAuthenticated` has already run, and the service re-reads `role`
        # off whatever it is handed, so nothing downstream trusts this annotation.
        owner = cast("User", request.user)

        media = services.upload_media(owner=owner, upload=serializer.validated_data["file"])

        # ⚠️ Nothing here catches `PayloadTooLarge`, `UnsupportedMediaType` or the corrupt-image
        # `UnprocessableEntity`. They are DRF `APIException`s carrying §6.4's codes, and
        # `urbenmend_exception_handler` already renders each into the §4.1 envelope. A local
        # `except` would only be a chance to get the status wrong.
        return Response(MediaResponseSerializer(media).data, status=status.HTTP_202_ACCEPTED)


class MediaDetailView(APIView):
    """`GET /media/{id}` (public) and `DELETE /media/{id}` (author or Admin) — API §6.4.

    ⚠️ **`permission_classes = [AllowAny]` and the `DELETE` is still protected.** Q7 resolved media
    visibility as public, so the read cannot require a session; the delete's rule is
    author-pre-triage-or-Admin, which is a *domain* check that `remove_media()` owns. An
    `IsAuthenticated` here would make the read unreachable, and a per-method permission class would
    put half the authorization in the view.

    ⚠️ **`authentication_classes` is NOT emptied.** Setting it to `[]` is what silently disables
    CSRF (the T1.3 trap `identity/tests/test_csrf.py` exists to catch), and this view has a mutating
    method on it. `AllowAny` skips the *permission* check while `SessionAuthentication` still runs,
    so an authenticated `DELETE` is still CSRF-protected and an anonymous `GET` still works.

    ⚠️ **An anonymous `DELETE` must be `401`, not `403`.** `AllowAny` lets `request.user` be
    `AnonymousUser`, so the method checks authentication itself and raises DRF's `NotAuthenticated`
    — which the handler rewrites back to `401` globally (§4.2). Calling `remove_media()` with an
    `AnonymousUser` would instead hit `.role` on a model that has none.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request, media_id: str) -> Response:
        """Public read. `404` when absent, `410` when moderated — both raised by the selector."""
        media = selectors.get_media_for_read(media_id=media_id)
        return Response(MediaResponseSerializer(media).data, status=status.HTTP_200_OK)

    def delete(self, request: Request, media_id: str) -> Response:
        """Remove from a pre-triage report (author) or by moderation (Admin) — `204` (§6.4)."""
        if not request.user or not request.user.is_authenticated:
            raise NotAuthenticated

        # ⚠️ The selector is reused, so a `DELETE` against a moderated photo answers `410` rather
        # than `204`. Idempotency belongs to a *repeat of your own* removal; being told "gone"
        # about content an Admin took down is the honest answer, and `remove_media()`'s
        # already-removed short-circuit covers the author's own retry before that state is reached.
        media = selectors.get_media_for_read(media_id=media_id)
        # ⚠️ No `cast` here, unlike `post()`: the `is_authenticated` guard above already narrows
        # `request.user` to `User`, and mypy reports a cast on top of it as redundant.
        services.remove_media(actor=request.user, media=media)
        return Response(status=status.HTTP_204_NO_CONTENT)
