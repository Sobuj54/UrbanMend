from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db


def _write_dataset(path: Path, *, severity: str = "critical") -> None:
    path.write_text(
        json.dumps(
            {
                "text": "A live wire is hanging over the road.",
                "language": "en",
                "expected_category": "electrical",
                "expected_severity": severity,
            }
        ),
        encoding="utf-8",
    )


def test_fallback_evaluation_prints_machine_readable_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "evaluation.jsonl"
    _write_dataset(dataset)

    call_command("evaluate_classifier", dataset, classifier="fallback")

    result = json.loads(capsys.readouterr().out)
    assert result["cases"] == 1
    assert result["category_accuracy"] == 1.0
    assert result["severity_agreement"] == 1.0
    assert result["failures"] == []


def test_evaluation_can_fail_a_release_gate(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.jsonl"
    _write_dataset(dataset, severity="low")

    with pytest.raises(CommandError, match="missed one or more"):
        call_command(
            "evaluate_classifier",
            dataset,
            classifier="fallback",
            fail_below_target=True,
        )


def test_evaluation_rejects_invalid_rows(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.jsonl"
    dataset.write_text('{"text": "missing labels"}', encoding="utf-8")

    with pytest.raises(CommandError, match="Invalid dataset row 1"):
        call_command("evaluate_classifier", dataset, classifier="fallback")
