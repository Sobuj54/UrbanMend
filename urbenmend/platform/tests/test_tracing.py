"""
Trace-id tests (A8, T0.9).

Covers the three surfaces a trace id has to reach — logs, the error body, the response header —
plus the propagation across the Celery enqueue boundary, which is the part DevOps §8.3 calls out
("so the span context crosses the enqueue boundary rather than restarting at the worker").

⚠️ Untrusted-input cases carry real weight here: the inbound header is echoed into a response
header and written to logs, so it is an injection surface, not a formatting concern.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from celery.signals import before_task_publish, task_postrun, task_prerun
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from urbenmend.platform.celery_tracing import adopt_trace_id, attach_trace_id, clear_trace_context
from urbenmend.platform.middleware import TraceIdMiddleware
from urbenmend.platform.tracing import (
    TRACE_ID_HEADER,
    TRACE_ID_TASK_HEADER,
    get_trace_id,
    new_trace_id,
    sanitize_inbound,
    set_trace_id,
)

factory = RequestFactory()


def run_middleware(inbound: str | None = None) -> HttpResponse:
    """Drive one request through the middleware with a trivial view.

    `inbound` is the caller-supplied `X-Trace-Id`, or `None` for a request that carries none.
    """
    middleware = TraceIdMiddleware(lambda _request: HttpResponse("ok"))
    headers = {} if inbound is None else {TRACE_ID_HEADER: inbound}
    return middleware(factory.get("/api/v1/health", headers=headers))


# ------------------------------------------------------------------------------------------
# Id generation
# ------------------------------------------------------------------------------------------
def test_ids_are_unique_and_opaque() -> None:
    """`uuid4().hex` — no host or timestamp, unlike uuid1."""
    ids = {new_trace_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(value) == 32 and value.isalnum() for value in ids)


def test_get_trace_id_never_returns_empty() -> None:
    """`traceId` is non-optional in the §4.1 envelope.

    A management command or a task started outside a request still has to log something
    joinable, so an unset contextvar yields a fresh id rather than `""`.
    """
    set_trace_id("")
    assert get_trace_id()


def test_a_generated_fallback_is_not_persisted() -> None:
    """The fallback must not win over a real id set later in the same context."""
    set_trace_id("")
    get_trace_id()
    set_trace_id("the-real-one")
    assert get_trace_id() == "the-real-one"


# ------------------------------------------------------------------------------------------
# Inbound sanitisation
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "0123456789abcdef",
        "a-uuid-with-dashes-0000",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # W3C traceparent.
    ],
)
def test_plausible_inbound_ids_are_adopted(value: str) -> None:
    """A proxy-supplied id is reused so one trace spans edge → API → worker (DevOps §8.3)."""
    assert sanitize_inbound(value) == value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("abc\ndef", "newline — would let a caller forge whole log entries"),
        ("abc\r\nX-Evil: 1", "CRLF — header splitting in the echoed response header"),
        ("abc def", "space — not a plausible id, and splits log parsing"),
        ("id\x00null", "NUL byte"),
        ("id\x7f", "DEL — non-printable"),
        ("", "empty"),
        ("   ", "whitespace only"),
        (None, "absent"),
        ("x" * 129, "over the length cap — unbounded log/header growth"),
    ],
)
def test_unsafe_inbound_ids_are_rejected(value: str | None, reason: str) -> None:
    """Rejected outright rather than escaped: a trace id has no reason to contain any of this.

    A rejected value means the server generates its own, so the request is still traceable.
    """
    assert sanitize_inbound(value) is None, reason


def test_a_rejected_inbound_id_does_not_reach_the_response_header() -> None:
    """End-to-end on the injection path, not just the helper.

    The middleware is where a bad value would actually land in a header, so the rejection is
    asserted where the consequence is.
    """
    response = run_middleware("evil\r\nX-Admin: true")
    assert response[TRACE_ID_HEADER] != "evil\r\nX-Admin: true"
    assert "\r" not in response[TRACE_ID_HEADER]
    assert "\n" not in response[TRACE_ID_HEADER]


# ------------------------------------------------------------------------------------------
# Middleware
# ------------------------------------------------------------------------------------------
def test_every_response_carries_the_trace_header() -> None:
    """Set on success too, not only on errors — a slow 200 needs correlating as well."""
    assert run_middleware()[TRACE_ID_HEADER]


def test_a_valid_inbound_header_is_echoed_back() -> None:
    assert run_middleware("from-the-edge")[TRACE_ID_HEADER] == "from-the-edge"


def test_the_request_gets_the_same_id_as_the_response() -> None:
    """`request.trace_id` lets a view or service read it without importing the contextvar."""
    captured: dict[str, Any] = {}

    def view(request: HttpRequest) -> HttpResponse:
        captured["id"] = request.trace_id  # type: ignore[attr-defined]
        captured["contextvar"] = get_trace_id()
        return HttpResponse("ok")

    response = TraceIdMiddleware(view)(factory.get("/api/v1/health"))

    assert captured["id"] == captured["contextvar"] == response[TRACE_ID_HEADER]


def test_log_context_is_bound_for_the_request() -> None:
    """The id in the log line is the id in the response — that link is the whole feature."""
    run_middleware("bound-id")
    assert structlog.contextvars.get_contextvars()["trace_id"] == "bound-id"


def test_the_query_string_is_not_logged() -> None:
    """`request.path`, never the full URI.

    Filters carry `?q=` search text, and NFR-12/P6 treat report content as personal data that
    must not land in logs.
    """
    middleware = TraceIdMiddleware(lambda _request: HttpResponse("ok"))
    middleware(factory.get("/api/v1/reports?q=secret-search-term"))

    bound = structlog.contextvars.get_contextvars()
    assert bound["path"] == "/api/v1/reports"
    assert "secret-search-term" not in str(bound)


def test_context_does_not_leak_between_requests() -> None:
    """Cleared on entry, not on exit.

    Under ASGI many requests share a context. A `finally`-only cleanup still leaks if a response
    short-circuits, and the result is one request's id on another's log lines — confidently wrong
    correlation that no error surfaces.
    """
    structlog.contextvars.bind_contextvars(trace_id="stale", leftover="from-the-last-request")

    run_middleware("fresh")

    bound = structlog.contextvars.get_contextvars()
    assert bound["trace_id"] == "fresh"
    assert "leftover" not in bound


# ------------------------------------------------------------------------------------------
# Celery propagation
# ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("signal", "receiver"),
    [
        (before_task_publish, attach_trace_id),
        (task_prerun, adopt_trace_id),
        (task_postrun, clear_trace_context),
    ],
)
def test_the_receivers_are_actually_connected_at_runtime(signal: Any, receiver: Any) -> None:
    """⚠️ The test that catches the failure every other test in this section misses.

    `@signal.connect` registers at import time, so the receivers only exist if something in the
    production import graph imports `celery_tracing`. Nothing did initially: the functions were
    correct, the tests below passed because they call them directly, and propagation was a no-op
    in both processes. This asserts the wiring rather than the function bodies.

    Connected via `urbenmend/celery.py`, which both the API and the worker load through
    `urbenmend/__init__.py` — the API is the publisher, so it needs the receivers too.
    """
    import urbenmend.celery  # noqa: F401  (ensures the side-effecting import has run)

    connected = [
        registered() if callable(registered) else registered for _, registered in signal.receivers
    ]
    assert receiver in connected or receiver.__name__ in {
        getattr(item, "__name__", "") for item in connected
    }


def test_the_publisher_stamps_its_id_into_the_message_headers() -> None:
    """⚠️ Into `headers`, not `body`.

    Under protocol v2 the body holds the task's arguments; putting the id there would change the
    task signature and break the receiving function.
    """
    set_trace_id("from-the-request")
    headers: dict[str, Any] = {}

    attach_trace_id(headers=headers)

    assert headers[TRACE_ID_TASK_HEADER] == "from-the-request"


def test_an_existing_task_header_is_not_overwritten() -> None:
    """A retry or a chained task keeps the id it was published with."""
    set_trace_id("current-context")
    headers = {TRACE_ID_TASK_HEADER: "original-publish"}

    attach_trace_id(headers=headers)

    assert headers[TRACE_ID_TASK_HEADER] == "original-publish"


def test_publishing_without_headers_is_a_no_op() -> None:
    """Celery may invoke the signal with `headers=None`; a crash there would fail the enqueue."""
    attach_trace_id(headers=None)


class FakeRequest:
    def __init__(self, headers: dict[str, Any] | None) -> None:
        self.headers = headers
        self.id = "task-1"


class FakeTask:
    name = "urbenmend.reporting.tasks.classify"

    def __init__(self, headers: dict[str, Any] | None) -> None:
        self.request = FakeRequest(headers)


def test_the_worker_adopts_the_publishers_id() -> None:
    """The join that makes a request traceable into the work it queued."""
    clear_trace_context()
    adopt_trace_id(task=FakeTask({TRACE_ID_TASK_HEADER: "from-the-api"}))

    assert get_trace_id() == "from-the-api"
    assert structlog.contextvars.get_contextvars()["trace_id"] == "from-the-api"


def test_a_beat_scheduled_task_still_gets_an_id() -> None:
    """`celery beat` has no originating request — the outbox relay (T6.2) still needs to log."""
    clear_trace_context()
    adopt_trace_id(task=FakeTask(None))
    assert get_trace_id()


def test_a_malicious_task_header_is_rejected_in_the_worker() -> None:
    """The broker is trusted, but the id may have originated at the edge.

    Sanitising only at the HTTP boundary would let a crafted value reach worker logs by way of a
    queued task.
    """
    clear_trace_context()
    adopt_trace_id(task=FakeTask({TRACE_ID_TASK_HEADER: "bad\nid"}))
    assert "\n" not in get_trace_id()


def test_task_context_is_bound_and_then_cleared() -> None:
    """A worker process runs many tasks in sequence.

    Leaving bindings in place attributes the next task's early log lines to this one.
    """
    adopt_trace_id(task=FakeTask({TRACE_ID_TASK_HEADER: "task-trace"}))
    bound = structlog.contextvars.get_contextvars()
    assert bound["task_name"] == "urbenmend.reporting.tasks.classify"
    assert bound["task_id"] == "task-1"

    clear_trace_context()
    assert structlog.contextvars.get_contextvars() == {}
