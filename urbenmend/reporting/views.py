"""
Reporting — HTTP layer (T2.2).

Views stay thin (Arch §2.4, R-12/DC-3): parse, call the service, render. Every rule the endpoint
enforces — Citizen-only, BR-2, BR-3, BR-35 — lives in `reporting/services.py`, which is the
enforcement point (FR-3).

[doc: API §6.3; FR-5, NFR-3]
"""

from __future__ import annotations

from typing import cast

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.identity.models import User
from urbenmend.reporting import services
from urbenmend.reporting.serializers import (
    LocationSerializer,
    ReportSubmitResponseSerializer,
    ReportSubmitSerializer,
)


class ReportSubmitView(APIView):
    """`POST /reports` — persist now, triage later (API §6.3, FR-5, NFR-3).

    ⚠️ **`202`, not `201`.** The row is durable when this returns, but the resource is
    *incomplete*: category, severity signal and Issue link are all empty until the worker finishes
    (Arch §4). A `201` with `Location` would promise a settled resource, and §6.3 fixes `202`.

    ⚠️ **No `Location` header** for the same reason plus a concrete one: `GET /reports/{id}` is
    T2.7, so the header would point at a route that answers `404` today.

    ⚠️ **No role check in `permission_classes`.** `IsAuthenticated` establishes *who*; the
    Citizen-only rule is `author.role != Role.CITIZEN` inside `create_report()` (FR-3). A DRF
    permission class here would read as the enforcement point and drift from it — the reasoning
    `ProvisionAuthorityView` records.

    ⚠️ **This endpoint ships unthrottled, deliberately and with a task against it.** FR-33
    submission rate limiting is **T2.9**. The T1.8 auth buckets are sized for credential guessing
    and are the wrong shape here: `auth_user` at 20/15m would cut off a legitimate citizen filing
    several potholes on one walk, and reusing it would also let report submissions exhaust the
    login allowance. Naming the gap beats silently borrowing a limit that does not fit.

    ⚠️ **T2.3: the `Idempotency-Key` header is read here and resolved in the service** (plan T2.3,
    §4.6). This layer does exactly two idempotency-related things — lift the header off the request,
    and turn the resulting `replayed` flag into the `Idempotency-Replayed` response header. Both
    are HTTP-shaped facts. Everything else (scope, fingerprint, reservation, replay) is a rule, so
    it lives in `submit_report()` where a non-HTTP caller gets it too.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
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
            # T2.6 wires the real count; §6.3's `mediaIds` is refused until then (see
            # `validate_media_ids`), so `0` is the truthful value, not an optimistic placeholder.
            media_count=0,
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
