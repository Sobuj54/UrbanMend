"""T2.2 — the triage task exists, is registered, and is registered under the *right name*.

Small suite, but it guards the one thing `mypy` structurally cannot: `@shared_task` is untyped, so
`classify_report.delay(...)` types as `Any` and a wrong argument at the enqueue site is invisible
to the type checker (see the `disallow_untyped_decorators` override in `pyproject.toml`). These
assertions are the substitute.

[doc: plan T2.2, T3.5; Arch §4; async-worker.md]
"""

from __future__ import annotations

import inspect

from urbenmend.celery import app
from urbenmend.classification.tasks import CLASSIFY_REPORT_TASK, classify_report


def test_task_is_registered_with_the_explicit_name() -> None:
    """⚠️ The name is the wire contract between the API and the worker.

    A message published under one name and consumed by a worker that registered another fails as
    `NotRegistered` — at run time, in the worker, after the deploy. Autodiscovery finding the module
    is not enough; the *name* has to match.
    """
    assert classify_report.name == CLASSIFY_REPORT_TASK
    assert CLASSIFY_REPORT_TASK in app.tasks


def test_task_name_is_not_the_module_path() -> None:
    """Asserted so the explicit `name=` is not "tidied away" as redundant.

    Dropping it makes the task's name follow its module path, so moving or renaming the module
    silently renames the task and orphans any message already queued under the old name.
    """
    assert classify_report.name != "urbenmend.classification.tasks.classify_report"


def test_task_takes_the_report_id_as_its_first_parameter() -> None:
    """⚠️ The signature check `mypy` cannot do — `.delay()` is `Any`-typed.

    Renaming or reordering this parameter breaks `submit_report()`'s enqueue with no type error and
    no test failure anywhere else, surfacing only when a worker rejects the message.
    """
    parameters = list(inspect.signature(classify_report).parameters)

    assert parameters[0] == "report_id"


def test_the_stub_neither_raises_nor_reports_success() -> None:
    """T3.5's body is not written yet, and the placeholder must fail neither way.

    ⚠️ Raising would turn "not implemented" into retry/alert noise on every submission; returning
    something truthy would invite a caller to treat it as a completed classification. It logs and
    returns `None` — and `test_submission.py` asserts the report stays `processing`, which is the
    honest state until T3.5 lands.
    """
    assert classify_report.run("00000000-0000-0000-0000-000000000000") is None
