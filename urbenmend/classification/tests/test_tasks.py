"""The classification task's stable Celery wire contract."""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize("outcome", ["classified", "already_classified"])
def test_completed_classification_runs_clustering(outcome: str) -> None:
    issue_id = uuid.uuid4()
    with (
        patch(
            "urbenmend.classification.services.classify_report_record",
            return_value=outcome,
        ),
        patch("urbenmend.issues.services.cluster_report", return_value=issue_id) as cluster,
    ):
        classify_report.run("report-id")

    cluster.assert_called_once_with("report-id")


@pytest.mark.parametrize("outcome", ["missing", "moderated", "ineligible"])
def test_ineligible_classification_outcomes_do_not_cluster(outcome: str) -> None:
    with (
        patch(
            "urbenmend.classification.services.classify_report_record",
            return_value=outcome,
        ),
        patch("urbenmend.issues.services.cluster_report") as cluster,
    ):
        classify_report.run("report-id")

    cluster.assert_not_called()


def test_stale_classification_requeues_without_clustering() -> None:
    with (
        patch(
            "urbenmend.classification.services.classify_report_record",
            return_value="stale",
        ),
        patch.object(classify_report, "delay") as delay,
        patch("urbenmend.issues.services.cluster_report") as cluster,
    ):
        classify_report.run("report-id")

    delay.assert_called_once_with("report-id")
    cluster.assert_not_called()


def test_clustering_failure_propagates_for_task_retry() -> None:
    failure = RuntimeError("database unavailable")
    with (
        patch(
            "urbenmend.classification.services.classify_report_record",
            return_value="classified",
        ),
        patch("urbenmend.issues.services.cluster_report", side_effect=failure),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        classify_report.run("report-id")


def test_classification_always_precedes_clustering() -> None:
    calls: list[str] = []

    def classify(_report_id: str) -> str:
        calls.append("classify")
        return "classified"

    def cluster(_report_id: str) -> uuid.UUID:
        calls.append("cluster")
        return uuid.uuid4()

    with (
        patch(
            "urbenmend.classification.services.classify_report_record",
            side_effect=classify,
        ),
        patch("urbenmend.issues.services.cluster_report", side_effect=cluster),
    ):
        classify_report.run("report-id")

    assert calls == ["classify", "cluster"]
