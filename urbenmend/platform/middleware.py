"""
Request-scoped observability middleware (A8, T0.9).

Binds a trace id to the request, to every log line it produces, and to the response header.
Registered near the top of `MIDDLEWARE` so that later middleware — including error paths — log
with the id already bound [doc: DevOps §8.3, Plan T0.9].
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse

from urbenmend.platform.tracing import (
    TRACE_ID_HEADER,
    TRACE_ID_META_KEY,
    new_trace_id,
    sanitize_inbound,
    set_trace_id,
)


class TraceIdMiddleware:
    """Assign or adopt a trace id for the lifetime of one request.

    ⚠️ `clear_contextvars()` runs at the start of every request, not the end. Under ASGI the
    same context can be reused, and a `finally`-only cleanup would still leave one request's
    bindings visible to the next if the response short-circuited. Clearing on entry makes the
    starting state unconditional.
    """

    sync_capable = True
    async_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        structlog.contextvars.clear_contextvars()

        trace_id = sanitize_inbound(request.META.get(TRACE_ID_META_KEY)) or new_trace_id()
        set_trace_id(trace_id)

        # `request.trace_id` so views/services can read it without importing the contextvar.
        request.trace_id = trace_id  # type: ignore[attr-defined]

        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            method=request.method,
            # `request.path`, never the query string — filters can carry `?q=` search text,
            # and NFR-12/P6 treat report content as personal data that should not land in logs.
            path=request.path,
        )

        response = self.get_response(request)

        # Echoed so a client (or a support ticket) can quote the id without a body to parse.
        # Set on every response, not just errors — a slow 200 needs correlating too.
        response[TRACE_ID_HEADER] = trace_id
        return response
