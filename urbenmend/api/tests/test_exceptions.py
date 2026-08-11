"""
Error-envelope tests (A8, T0.6).

The envelope is a *contract*, not an implementation detail — API §4.1 is authoritative over the
code, and every client parses this shape. So these assert the JSON keys themselves rather than
the handler's internals: a refactor that keeps the shape must pass, and one that renames a key
must fail.

⚠️ No database access anywhere in this module. `identity/0001` does not exist yet (A7 is
outstanding), so a DB-touching test would error for a reason unrelated to what it checks.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import (
    APIException,
    ErrorDetail,
    NotAuthenticated,
    Throttled,
    ValidationError,
)
from rest_framework.exceptions import (
    PermissionDenied as DRFPermissionDenied,
)

from urbenmend.api.exceptions import urbenmend_exception_handler
from urbenmend.platform.tracing import set_trace_id


def handle(exc: Exception) -> Any:
    """Invoke the handler with the empty context DRF passes for a plain view."""
    response = urbenmend_exception_handler(exc, {})
    assert response is not None, "handler returned None for an exception DRF recognises"
    return response


def test_every_error_carries_code_message_and_trace_id() -> None:
    """API §4.1: `code`, `message` and `traceId` are non-optional on every error."""
    set_trace_id("trace-under-test")

    response = handle(NotAuthenticated())

    assert set(response.data) == {"error"}
    error = response.data["error"]
    assert error["code"] == "UNAUTHENTICATED"
    assert error["traceId"] == "trace-under-test"
    assert isinstance(error["message"], str) and error["message"]


def test_trace_id_matches_the_request_scoped_id() -> None:
    """The body's `traceId` is the middleware's id, not a fresh one.

    This is the whole point of the field: a user quotes it from a screenshot and support finds
    the matching log lines. A newly generated id here would look correct and correlate to
    nothing.
    """
    set_trace_id("shared-with-the-log-lines")
    assert handle(Http404()).data["error"]["traceId"] == "shared-with-the-log-lines"


@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_code"),
    [
        (NotAuthenticated(), http_status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED"),
        (DRFPermissionDenied(), http_status.HTTP_403_FORBIDDEN, "FORBIDDEN"),
        (Http404(), http_status.HTTP_404_NOT_FOUND, "NOT_FOUND"),
        (ValidationError("bad"), http_status.HTTP_400_BAD_REQUEST, "VALIDATION_FAILED"),
        (Throttled(), http_status.HTTP_429_TOO_MANY_REQUESTS, "RATE_LIMITED"),
    ],
)
def test_status_maps_to_the_documented_code(
    exc: Exception, expected_status: int, expected_code: str
) -> None:
    """API §4.3's base codes, each on its documented status."""
    response = handle(exc)
    assert response.status_code == expected_status
    assert response.data["error"]["code"] == expected_code


def test_django_exceptions_are_translated_not_leaked() -> None:
    """`Http404`/`PermissionDenied` raised in a service still produce the envelope.

    `services.py` should not import DRF (Arch §3.1), so it raises Django's exceptions. Without
    translation those bypass DRF and render Django's HTML error page — an HTML body from a JSON
    API (API §1.2).
    """
    assert handle(Http404()).data["error"]["code"] == "NOT_FOUND"
    assert handle(PermissionDenied()).data["error"]["code"] == "FORBIDDEN"
    assert handle(DjangoValidationError("nope")).status_code == http_status.HTTP_400_BAD_REQUEST


def test_a_service_raised_field_error_keeps_its_field_name() -> None:
    """⚠️ **`exc.message_dict`, not `exc.messages`** — found in T2.6, and it was a live contract gap.

    `services.py` raises Django's `ValidationError({"field": "..."})` rather than DRF's (Arch §3.1),
    and `.messages` flattens a mapping to a bare list of its values. So every dict-shaped service
    error rendered as a §4.1 `details` entry with **no `field` key at all** — the client got the
    right status and no way to know which input to fix. It went unnoticed because the field name in
    T2.2's tests came from the *serializer*, where the camelCase layer supplies it.
    """
    exc = DjangoValidationError({"category": "'bridges' is not an active category."})

    details = handle(exc).data["error"]["details"]

    assert len(details) == 1
    assert details[0]["field"] == "category"
    assert details[0]["message"] == "'bridges' is not an active category."


def test_a_service_raised_field_name_is_camel_cased() -> None:
    """⚠️ The other half of the same fix: no serializer is in the loop for a service-raised error.

    A service's dict keys are `snake_case` — they name model columns and function parameters — so
    `media_ids` would otherwise reach a contract that says `mediaIds` (§1.2). The keys are field
    names rather than content, which is what makes rewriting them safe.
    """
    exc = DjangoValidationError({"media_ids": "A report may carry at most 5 photos."})

    assert handle(exc).data["error"]["details"][0]["field"] == "mediaIds"


def test_a_list_shaped_service_error_does_not_crash_the_handler() -> None:
    """⚠️ `hasattr(exc, "error_dict")` is the documented discriminator, and it has to be.

    Django only sets `error_dict` when the exception was built from a mapping; reading
    `.message_dict` on a list-shaped one raises `AttributeError` **from inside the error handler**,
    which turns a tidy `400` into an unhandled `500` on the error path itself.
    """
    response = handle(DjangoValidationError(["Something was wrong.", "And another thing."]))

    assert response.status_code == http_status.HTTP_400_BAD_REQUEST
    details = response.data["error"]["details"]
    assert len(details) == 2
    # No field to name — §4.1 omits the key rather than inventing one.
    assert "field" not in details[0]


def test_validation_details_are_flat_with_field_issue_and_message() -> None:
    """API §4.1's `details` is a flat array of `{field, issue, message}`.

    `issue` comes from DRF's `ErrorDetail.code` — the machine-readable token the contract wants
    (`REQUIRED`, `INVALID`, …). Serializer field errors carry it automatically; the example in
    §4.1 (`{"field": "location", "issue": "REQUIRED"}`) is exactly this path.
    """
    exc = ValidationError({"location": [ErrorDetail("A location is required.", code="required")]})

    details = handle(exc).data["error"]["details"]

    assert len(details) == 1
    assert details[0]["field"] == "location"
    assert details[0]["issue"] == "REQUIRED"
    assert details[0]["message"] == "A location is required."


def test_a_bare_string_leaf_falls_back_to_invalid() -> None:
    """A service raising `ValidationError("...")` with no code still yields a usable `issue`.

    `issue` is non-optional in the §4.1 example, so the fallback matters: a plain Python string
    has no `.code`, and omitting the key would leave clients switching on prose.
    """
    exc = ValidationError({"note": ["Something was wrong."]})
    assert handle(exc).data["error"]["details"][0]["issue"] == "INVALID"


def test_a_service_chosen_code_survives() -> None:
    """A domain code set by a service reaches the client verbatim.

    §4.2's state-conflict and business-rule codes (`NOT_EDITABLE`, `INVALID_TRANSITION`,
    `ALREADY_CONFIRMED`, `OUT_OF_CITY`) are the reason `default_code` is consulted at all. The
    generic status fallback must not overwrite them.
    """
    exc = APIException(detail="This report is no longer editable.", code="not_editable")
    exc.status_code = http_status.HTTP_409_CONFLICT

    assert handle(exc).data["error"]["code"] == "NOT_EDITABLE"


def test_a_generic_drf_code_does_not_reach_the_body() -> None:
    """DRF's `permission_denied` never appears as the `code`.

    API §4.3 fixes the base vocabulary. `PERMISSION_DENIED` is not in it, and a client written
    against the spec would fall through its `switch` on an unknown value.
    """
    assert handle(DRFPermissionDenied()).data["error"]["code"] == "FORBIDDEN"
    assert handle(NotAuthenticated()).data["error"]["code"] == "UNAUTHENTICATED"


def test_nested_validation_errors_flatten_to_dotted_paths() -> None:
    """A nested serializer's errors become `parent.child`, not a nested mirror.

    The dotted path is what lets a client highlight the offending input; handing back DRF's
    nested structure would push that mapping onto every client.
    """
    exc = ValidationError({"location": {"lng": ["Invalid."]}})
    assert handle(exc).data["error"]["details"][0]["field"] == "location.lng"


def test_many_serializer_errors_carry_the_list_index() -> None:
    """`media.0.file` — the index identifies which item of a `many=True` payload failed."""
    exc = ValidationError({"media": [{"file": ["Unsupported."]}]})
    assert handle(exc).data["error"]["details"][0]["field"] == "media.0.file"


def test_details_is_omitted_when_there_is_nothing_to_say() -> None:
    """Not `[]`. API §1.2 omits a field with no content; an empty array invites clients to
    render an empty error list."""
    assert "details" not in handle(NotAuthenticated()).data["error"]


def test_retry_after_header_survives_the_rebuilt_body() -> None:
    """API §4.5 requires `Retry-After` on a 429.

    The handler replaces `response.data` wholesale, so a header DRF attached to the exception is
    easy to drop — and a client with no `Retry-After` retries immediately and stays limited.
    """
    response = handle(Throttled(wait=30))
    assert "Retry-After" in response


def test_unrecognised_exceptions_are_not_swallowed() -> None:
    """Returning `None` hands a genuine bug back to Django.

    That keeps the `DEBUG` traceback locally and the error report in deployment. Wrapping it in
    a tidy 500 envelope would hide crashes behind a well-formed body.
    """
    assert urbenmend_exception_handler(RuntimeError("a real bug"), {}) is None
