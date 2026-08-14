"""Classification then clustering in one asynchronous triage task (T3.5, T4.5)."""

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
    """Classify one Report, then cluster it only when classification is complete.

    Args:
        report_id: The `Report.pk` as a string. ⚠️ **A string, not a `UUID` and not the model
            instance.** kombu's JSON encoder will happily serialize a `UUID`, but it arrives at
            the worker as a `str`, so the annotation would be a lie on the receiving side. Passing
            the *instance* would be worse: it puts a whole row on the broker, and the worker would
            then act on a snapshot taken before commit instead of re-reading the committed row.

    ⚠️ Imports stay inside the body: `reporting.services` imports this task for enqueueing,
    while `issues.services` imports `Report`. Moving either service import to module scope closes
    that loop during Django startup.

    ⚠️ **T3.5 must re-read the row and re-check its status**, not trust the id it was handed.
    At-least-once delivery means this can run twice for one report, and the report may have been
    moderated (`hidden`/`removed`) between enqueue and execution.
    """
    from urbenmend.classification.services import classify_report_record
    from urbenmend.issues.services import cluster_report

    outcome = classify_report_record(report_id)
    if outcome == "stale":
        # The citizen edited the report while an external call was in flight. Requeue the committed
        # latest version instead of persisting a classification for stale text.
        classify_report.delay(report_id)
        logger.info("classification.report_requeued", report_id=report_id, reason="stale_input")
        return

    issue_id = None
    if outcome in {"classified", "already_classified"}:
        # T4.5: classification must commit first because clustering requires category and severity.
        # A clustering exception intentionally propagates. Classification is durable, so retrying
        # this task returns `already_classified` and idempotently retries only this step.
        issue_id = cluster_report(report_id)

    logger.info(
        "classification.report_finished",
        report_id=report_id,
        outcome=outcome,
        issue_id=None if issue_id is None else str(issue_id),
    )
