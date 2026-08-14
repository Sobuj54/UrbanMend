"""The classification task's stable Celery wire contract."""

from __future__ import annotations

import inspect

from urbenmend.celery import app
from urbenmend.classification.tasks import CLASSIFY_REPORT_TASK, classify_report


def test_task_is_registered_with_the_explicit_name() -> None:
    assert classify_report.name == CLASSIFY_REPORT_TASK
    assert CLASSIFY_REPORT_TASK in app.tasks


def test_task_name_is_not_the_module_path() -> None:
    assert classify_report.name != "urbenmend.classification.tasks.classify_report"


def test_task_takes_the_report_id_as_its_first_parameter() -> None:
    parameters = list(inspect.signature(classify_report).parameters)

    assert parameters[0] == "report_id"


def test_an_invalid_report_id_is_a_safe_no_op() -> None:
    """Malformed broker input is ignored without turning into a retry storm."""
    assert classify_report.run("not-a-uuid") is None
