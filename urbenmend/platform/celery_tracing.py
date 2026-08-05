"""
Trace-id propagation across the enqueue boundary (A8, T0.9).

Without this, a task's logs carry a fresh id and the request that queued the work cannot be
joined to the work itself — exactly the correlation DevOps §8.3 asks for ("so the span context
crosses the enqueue boundary rather than restarting at the worker").

Implemented with Celery's `before_task_publish` / `task_prerun` signals rather than a custom
Task base class, because `@shared_task` in application code would otherwise have to name that
base every time and one omission would silently drop the id.

⚠️ Not OpenTelemetry. DevOps §8.3 names the OTel Django/Celery instrumentations and an OTLP
backend, and **no tracing backend is provisioned** (the host is unpinned — see CLAUDE.md).
This carries the API §4.1 `traceId` through logs, which is the part T0.9 requires; adopting
OTel later replaces this module rather than fighting it.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery.signals import before_task_publish, task_postrun, task_prerun

from urbenmend.platform.tracing import (
    TRACE_ID_TASK_HEADER,
    get_trace_id,
    new_trace_id,
    sanitize_inbound,
    set_trace_id,
)


@before_task_publish.connect
def attach_trace_id(headers: dict[str, Any] | None = None, **_kwargs: Any) -> None:
    """Stamp the publisher's trace id into the outgoing message headers.

    ⚠️ Mutates `headers`, not `body`. Under Celery's protocol v2 the task's positional and
    keyword arguments live in the body while `headers` carries the message metadata — putting
    the id in the body would change the task signature and break the receiving function.
    """
    if headers is None:
        return
    headers.setdefault(TRACE_ID_TASK_HEADER, get_trace_id())


@task_prerun.connect
def adopt_trace_id(task: Any = None, **_kwargs: Any) -> None:
    """Rebind the publisher's id in the worker process, then log the task start.

    Falls back to a new id when the message has no header — a task queued by `celery beat`
    (the outbox relay, T6.2) has no originating request, and it still needs a joinable id.
    """
    inbound: str | None = None
    request = getattr(task, "request", None)
    if request is not None:
        headers = getattr(request, "headers", None) or {}
        inbound = sanitize_inbound(headers.get(TRACE_ID_TASK_HEADER))

    trace_id = inbound or new_trace_id()
    set_trace_id(trace_id)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        task_name=getattr(task, "name", "unknown"),
        task_id=getattr(request, "id", None),
    )


@task_postrun.connect
def clear_trace_context(**_kwargs: Any) -> None:
    """Unbind after the task.

    A worker process handles many tasks in sequence; leaving the bindings in place would
    attribute the next task's early log lines to this one.
    """
    structlog.contextvars.clear_contextvars()
