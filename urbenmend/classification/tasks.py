"""
Classification — Celery tasks (T2.2 creates the enqueue target; **T3.5 owns the body**).

`POST /reports` must hand triage off to the worker (Arch §4, FR-5, NFR-3), and you cannot enqueue
a job that does not exist. This module is therefore the *seam* T2.2 needs, deliberately thin: the
real classify-and-persist step is T3.5, which depends on the T3.1–T3.4 adapter chain (a
provider-agnostic ABC, the hosted-LLM adapter, the keyword fallback, and the cost cap) — none of
which exists yet, and ❓Q9 defers the provider.

⚠️ **The stub logs at warning level rather than doing nothing quietly.** An operator who starts a
worker today and sees reports sitting at `processing` needs a log line that says why. A silent
`pass` would look identical to a broken broker, a missing queue, or an autodiscovery typo.

⚠️ **It does not raise, either.** A raising stub would be retried (or dead-lettered) per the
worker's policy, turning "not implemented yet" into recurring alert noise that says nothing new.
Reports stay `processing` until T3.5 lands, which is the honest state.

[doc: Arch §4, §6; plan T2.2, T3.5; async-worker.md]
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)

# ⚠️ **Explicit name, not the module-path default.** Celery would otherwise register this as
# `urbenmend.classification.tasks.classify_report`, so moving or renaming the module renames the
# task — and any message already sitting in Redis under the old name fails on the worker as
# `NotRegistered`, after the deploy that caused it. The name is part of the wire contract between
# the API and the worker; pinning it here makes that explicit.
CLASSIFY_REPORT_TASK = "classification.classify_report"


@shared_task(name=CLASSIFY_REPORT_TASK)
def classify_report(report_id: str, **_options: Any) -> None:
    """Triage one Report: assign category, severity signal, confidence and source (FR-10).

    Args:
        report_id: The `Report.pk` as a string. ⚠️ **A string, not a `UUID` and not the model
            instance.** kombu's JSON encoder will happily serialize a `UUID`, but it arrives at
            the worker as a `str`, so the annotation would be a lie on the receiving side. Passing
            the *instance* would be worse: it puts a whole row on the broker, and the worker would
            then act on a snapshot taken before commit instead of re-reading the committed row.

    ⚠️ **T3.5 implements this. Import `Report` inside the body when it does, not at module
    scope** — `reporting/services.py` imports this module for the enqueue, so a module-level
    `from urbenmend.reporting...` closes the loop into a circular import. The failure surfaces at
    Django startup as a partially-initialised module, which reads as an unrelated
    `AttributeError`.

    ⚠️ **T3.5 must re-read the row and re-check its status**, not trust the id it was handed.
    At-least-once delivery means this can run twice for one report, and the report may have been
    moderated (`hidden`/`removed`) between enqueue and execution.
    """
    logger.warning(
        "classification.not_implemented",
        report_id=report_id,
        detail="Report enqueued for triage; the classifier lands with T3.5.",
    )
