"""
Uniform error envelope (A8, T0.6).

Every error response — DRF's own and ours — is rendered as API §4.1:

    {"error": {"code": ..., "message": ..., "details": [...], "traceId": ...}}

DRF's default handler emits `{"detail": ...}` for APIException and a bare field→errors dict for
ValidationError. Two different shapes, neither of them the contract, so `EXCEPTION_HANDLER` is
replaced rather than supplemented [doc: API §4.1, Plan T0.6].

⚠️ `traceId` is read from the contextvar the T0.9 middleware sets, so the id in the response
body is the same one in the log line for that request. Support can go from a screenshot to the
logs with it; a freshly generated id here would break that link silently.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, NotAuthenticated, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from urbenmend.platform.tracing import get_trace_id

# --------------------------------------------------------------------------------------
# Domain exceptions for the status codes API §4.2 names but DRF has no built-in for.
#
# Services raise Django-native exceptions (`PermissionDenied`, `ValidationError`) and the
# handler below translates them, which keeps `services.py` free of DRF imports. These two
# cover the cases where that translation cannot infer the right status: a state conflict is
# a 409, and a business-rule violation is a 422, but both arrive as ordinary exceptions.
#
# ⚠️ `code` is passed through to the envelope verbatim, so pass the spec's code
# (`ALREADY_CONFIRMED`, `OUT_OF_CITY`, `INVALID_TRANSITION`) when the spec names one.
# Defaults are the §4.3 generic buckets.
# --------------------------------------------------------------------------------------


class Conflict(APIException):
    """`409` — state conflict. API §4.2: NOT_EDITABLE, INVALID_TRANSITION, ALREADY_CONFIRMED."""

    status_code = http_status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "CONFLICT"


class UnprocessableEntity(APIException):
    """`422` — business-rule violation, e.g. OUT_OF_CITY (C-11) or an expired code."""

    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "The request could not be processed."
    default_code = "VALIDATION_FAILED"


class InvalidCredentials(APIException):
    """`401 UNAUTHENTICATED` — the generic login failure (API §6.1).

    ⚠️ **Deliberately not DRF's `NotAuthenticated` or `AuthenticationFailed`.** `APIView
    .handle_exception` special-cases those two: it asks the view's authenticators for a
    `WWW-Authenticate` header and, finding none, **rewrites the status to `403`**. Both
    conditions hold on `LoginView` — `authentication_classes` is empty, and
    `SessionAuthentication.authenticate_header()` returns `None` in any case — so using
    either class would turn every bad-password reply into a `403` that the spec says must
    be a `401`. A plain `APIException` is not touched by that branch.
    """

    status_code = http_status.HTTP_401_UNAUTHORIZED
    default_detail = "Invalid credentials."
    default_code = "UNAUTHENTICATED"


class AccountLocked(APIException):
    """`403 ACCOUNT_LOCKED` — correct password, but the account may not sign in (FR-4).

    ⚠️ API §6.1 specifies this as a "`423`-equivalent surfaced as `403 ACCOUNT_LOCKED`".
    `423 Locked` is a WebDAV status, not part of core HTTP, so the contract carries the
    distinction in the error `code` rather than the status line. Do not "correct" this to
    `423` — the status is the spec's deliberate choice, and `_STATUS_TO_CODE` has no entry
    for 423 precisely because none is wanted.

    DRF's own `PermissionDenied` cannot express it: its `default_code` is in
    `_DRF_DEFAULT_CODES`, so the handler would flatten this to the generic `FORBIDDEN` and
    the client would lose the one signal that tells "wrong password" apart from "your
    account has been suspended" — the difference between retrying and contacting support.
    """

    status_code = http_status.HTTP_403_FORBIDDEN
    default_detail = "This account is not permitted to sign in."
    default_code = "ACCOUNT_LOCKED"


# API §4.3 base codes, plus the status-specific codes §4.2 names. Anything not listed falls
# back to the generic bucket for its status class, so an unmapped status can never produce a
# response with no `code` at all.
_STATUS_TO_CODE: dict[int, str] = {
    http_status.HTTP_400_BAD_REQUEST: "VALIDATION_FAILED",
    http_status.HTTP_401_UNAUTHORIZED: "UNAUTHENTICATED",
    http_status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    http_status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    http_status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    http_status.HTTP_406_NOT_ACCEPTABLE: "NOT_ACCEPTABLE",
    http_status.HTTP_409_CONFLICT: "CONFLICT",
    http_status.HTTP_410_GONE: "GONE",
    http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "UNSUPPORTED_MEDIA_TYPE",
    http_status.HTTP_422_UNPROCESSABLE_ENTITY: "VALIDATION_FAILED",
    http_status.HTTP_429_TOO_MANY_REQUESTS: "RATE_LIMITED",
    http_status.HTTP_500_INTERNAL_SERVER_ERROR: "INTERNAL",
    http_status.HTTP_503_SERVICE_UNAVAILABLE: "DEPENDENCY_UNAVAILABLE",
}

_GENERIC_MESSAGE = "An unexpected error occurred."

# ⚠️ DRF's own `default_code` values, which must NOT reach a response body.
#
# The status map above is authoritative — API §4.3 fixes `UNAUTHENTICATED` and `FORBIDDEN`, while
# DRF's defaults are `not_authenticated` and `permission_denied`. Uppercasing those produces
# codes no client is written against and the spec does not contain.
#
# `default_code` is still honoured for anything NOT in this set, because that is a code a
# service chose deliberately — `NOT_EDITABLE`, `INVALID_TRANSITION`, `ALREADY_CONFIRMED`,
# `OUT_OF_CITY` (API §4.2, BR-5/BR-16/BR-23, C-11). Those are the whole reason the field exists.
_DRF_DEFAULT_CODES = frozenset(
    {
        "error",
        "invalid",
        "parse_error",
        "authentication_failed",
        "not_authenticated",
        "permission_denied",
        "not_found",
        "method_not_allowed",
        "not_acceptable",
        "unsupported_media_type",
        "throttled",
    }
)


def _code_for_status(code: int) -> str:
    if code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[code]
    return "VALIDATION_FAILED" if code < 500 else "INTERNAL"


def _flatten_validation_detail(detail: Any, field: str | None = None) -> list[dict[str, str]]:
    """Turn DRF's nested ValidationError detail into the flat `details` list of API §4.1.

    DRF nests arbitrarily deep — dicts for object fields, lists for `many=True` serializers,
    plain strings at the leaves. The contract is one flat array of
    `{field, issue, message}`, so nested paths are joined with dots (`location.lng`) and list
    positions become indices (`media.0.file`). That dotted path is what a client needs to
    highlight the offending input; a nested mirror of DRF's structure would push that mapping
    work onto every client instead.
    """
    # `child` is `str | None`, not `str`: a top-level non-field error (DRF's
    # `non_field_errors`, or a list body) has no field path to report, and the contract omits
    # `field` in that case rather than inventing a name for it.
    child: str | None

    if isinstance(detail, dict):
        out: list[dict[str, str]] = []
        for key, value in detail.items():
            child = str(key) if field is None else f"{field}.{key}"
            out.extend(_flatten_validation_detail(value, child))
        return out

    if isinstance(detail, list):
        out = []
        for index, value in enumerate(detail):
            # Only index when the list holds structures. A field's errors are usually a flat
            # list of strings, and `email.0` would be a worse pointer than `email`.
            child = field if not isinstance(value, dict | list) else f"{field}.{index}"
            out.extend(_flatten_validation_detail(value, child))
        return out

    # Leaf. DRF's ErrorDetail is a str subclass carrying `.code` — that is the machine-readable
    # `issue` the contract wants (`REQUIRED`, `INVALID`, …), so it is preferred over guessing
    # from the message text.
    issue = getattr(detail, "code", None) or "INVALID"
    entry = {"issue": str(issue).upper(), "message": str(detail)}
    if field is not None:
        entry["field"] = field
    return [entry]


def _build_body(
    *,
    code: str,
    message: str,
    details: list[dict[str, str]] | None,
    trace_id: str,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    # `details` is omitted rather than sent as `[]` — API §1.2 allows omitting a field that has
    # nothing to say, and an empty array invites clients to render an empty error list.
    if details:
        error["details"] = details
    error["traceId"] = trace_id
    return {"error": error}


def urbenmend_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """DRF `EXCEPTION_HANDLER` — see `REST_FRAMEWORK` in `settings/base.py`.

    Returns `None` for anything DRF does not recognise, which is deliberate: that hands the
    exception back to Django so `DEBUG` still shows a traceback locally and the 500 is still
    reported by error tracking in deployment. Swallowing it into a tidy JSON body would hide
    real bugs. ⚠️ The unhandled-500 path therefore does **not** carry the §4.1 envelope; the
    T0.9 middleware is what puts `traceId` on it.
    """
    # Django-level exceptions DRF understands only after translation. Handled before calling
    # the default handler so `Http404` from `get_object_or_404` inside a service still lands
    # in this envelope rather than as Django's HTML 404 page.
    if isinstance(exc, Http404):
        exc = APIException(detail="Not found.", code="NOT_FOUND")
        exc.status_code = http_status.HTTP_404_NOT_FOUND
    elif isinstance(exc, PermissionDenied):
        exc = APIException(detail="You do not have permission to perform this action.")
        exc.status_code = http_status.HTTP_403_FORBIDDEN
    elif isinstance(exc, DjangoValidationError):
        # A service raising Django's ValidationError (the natural choice in `services.py`,
        # which should not import DRF) still produces the contract shape.
        exc = ValidationError(detail=exc.messages)
    # ⚠️ DRF rewrites `NotAuthenticated` to 403 when no authenticator offers a
    # `WWW-Authenticate` header — which `SessionAuthentication` never does (Django #20760,
    # django-rest-framework #6021). API §4.2 fixes the distinction: `401 UNAUTHENTICATED`
    # means "show me a credential", `403 FORBIDDEN` means "I see who you are and you may
    # not". Undoing the rewrite at this point applies the correction to every protected
    # endpoint at once rather than type-ignoring each view's `handle_exception` override.
    elif isinstance(exc, NotAuthenticated) and exc.status_code == http_status.HTTP_403_FORBIDDEN:
        exc.status_code = http_status.HTTP_401_UNAUTHORIZED

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    trace_id = get_trace_id()
    status_code = response.status_code

    if isinstance(exc, ValidationError):
        details = _flatten_validation_detail(exc.detail)
        # 400 for a malformed body; a business-rule violation is 422 and the service raising it
        # sets that itself (API §4.2, e.g. OUT_OF_CITY per C-11).
        body = _build_body(
            code=_code_for_status(status_code),
            message="The request could not be validated.",
            details=details,
            trace_id=trace_id,
        )
    else:
        detail = getattr(exc, "detail", None)
        message = str(detail) if detail is not None else _GENERIC_MESSAGE
        # ⚠️ `detail.code` first, `default_code` second — NOT the other way round.
        # `APIException(detail=..., code="not_editable")` stores that code on the `ErrorDetail`
        # in `detail`; `default_code` stays the class-level default. Reading only `default_code`
        # would silently discard every code a service chose and report the generic status code
        # instead, so `409 NOT_EDITABLE` would flatten to `409 CONFLICT` (API §4.2).
        raw_code = getattr(detail, "code", None) or getattr(exc, "default_code", None)
        code = _code_for_status(status_code)
        if raw_code and str(raw_code) not in _DRF_DEFAULT_CODES:
            code = str(raw_code).upper()
        body = _build_body(code=code, message=message, details=None, trace_id=trace_id)

    response.data = body
    # Preserve headers DRF set on the exception — `Retry-After` on 429 (API §4.5) and
    # `WWW-Authenticate` on 401 are part of the contract and are lost if the body is
    # rebuilt without them.
    return response
