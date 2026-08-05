"""
Correlation / trace IDs (A8, T0.9).

One id per request, surfaced three ways so they can be joined after the fact:

1. every log line for that request (`structlog.contextvars`, already in the processor chain);
2. the `traceId` field of every error body (API §4.1);
3. the `X-Trace-Id` response header, so a client can quote it without parsing the body.

⚠️ A `contextvars.ContextVar`, not thread-local storage. Under ASGI (uvicorn, Arch §2.3) many
requests share a thread, and a thread-local would leak one request's id into another's logs —
the failure is silent and produces confidently wrong correlations.

[doc: DevOps §8.3, Plan T0.9]
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

# The header a proxy/ingress may already have set. Reused when present so a trace spans the
# whole edge→API→worker path instead of restarting here [doc: DevOps §8.3].
TRACE_ID_HEADER = "X-Trace-Id"
# Django's WSGI/ASGI meta key for that header.
TRACE_ID_META_KEY = "HTTP_X_TRACE_ID"
# Celery message header — task code reads it back through `self.request.headers`.
TRACE_ID_TASK_HEADER = "trace_id"

_MAX_INBOUND_LENGTH = 128

_trace_id: ContextVar[str] = ContextVar("urbenmend_trace_id", default="")


def new_trace_id() -> str:
    """A fresh id. `uuid4().hex` — opaque, and no host/timestamp leak as with uuid1."""
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def get_trace_id() -> str:
    """The current id, or a new one if nothing set it.

    Never returns empty: `traceId` is non-optional in the API §4.1 envelope, and a management
    command or a task started outside a request still has to log something joinable. The
    generated id is not stored, so a caller that later sets a real one wins.
    """
    return _trace_id.get() or new_trace_id()


def sanitize_inbound(value: str | None) -> str | None:
    """Accept a caller-supplied id only if it is safe to log and echo.

    ⚠️ This value reaches log files and a response header, so it is untrusted input. Newlines
    would let a caller forge log entries (log injection) and CR/LF would allow header
    splitting; anything non-printable-ASCII is rejected outright rather than escaped, because
    a trace id has no reason to contain it. A rejected value means we generate our own.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_INBOUND_LENGTH:
        return None
    # Printable ASCII excluding space and DEL. Covers hex ids, UUIDs with dashes, and W3C
    # traceparent strings.
    if not all("!" <= char <= "~" for char in candidate):
        return None
    return candidate
