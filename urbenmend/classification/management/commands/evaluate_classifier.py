from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from urbenmend.classification.contracts import ClassificationRequest, Severity, parse_severity
from urbenmend.classification.selectors import active_category_slugs
from urbenmend.classification.services import build_keyword_fallback, build_llm_classifier


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    text: str
    language: str
    expected_category: str
    expected_severity: Severity


def _load_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CommandError(f"Cannot read evaluation dataset {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        try:
            value: Any = json.loads(raw_line)
            if not isinstance(value, dict):
                raise ValueError("row must be a JSON object")
            cases.append(
                EvaluationCase(
                    text=str(value["text"]),
                    language=str(value.get("language", "en")),
                    expected_category=str(value["expected_category"]),
                    expected_severity=parse_severity(value["expected_severity"]),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CommandError(f"Invalid dataset row {line_number}: {exc}") from exc
    if not cases:
        raise CommandError("Evaluation dataset contains no cases")
    return cases


class Command(BaseCommand):
    help = "Evaluate the configured LLM or keyword fallback against a labeled JSONL dataset."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("dataset", type=Path)
        parser.add_argument("--classifier", choices=("llm", "fallback"), default="llm")
        parser.add_argument("--category-target", type=float, default=0.85)
        parser.add_argument("--severity-target", type=float, default=0.80)
        parser.add_argument(
            "--fail-below-target",
            action="store_true",
            help="Exit non-zero when either configured agreement target is missed.",
        )

    def handle(self, *args: object, **options: object) -> None:
        dataset = Path(options["dataset"])
        cases = _load_cases(dataset)
        classifier = (
            build_llm_classifier() if options["classifier"] == "llm" else build_keyword_fallback()
        )
        categories = tuple(active_category_slugs())
        if not categories:
            raise CommandError("No active categories are configured")

        category_matches = 0
        severity_matches = 0
        failures: list[dict[str, object]] = []
        for index, case in enumerate(cases, start=1):
            if case.expected_category not in categories:
                raise CommandError(
                    f"Dataset row {index} expects inactive category {case.expected_category!r}"
                )
            result = classifier.classify(
                ClassificationRequest(
                    text=case.text,
                    language=case.language,
                    allowed_categories=categories,
                )
            )
            category_ok = result.category == case.expected_category
            severity_ok = result.severity == case.expected_severity
            category_matches += int(category_ok)
            severity_matches += int(severity_ok)
            if not (category_ok and severity_ok):
                failures.append(
                    {
                        "row": index,
                        "expected_category": case.expected_category,
                        "actual_category": result.category,
                        "expected_severity": case.expected_severity.value,
                        "actual_severity": result.severity.value,
                        "model": result.model,
                    }
                )

        total = len(cases)
        category_accuracy = category_matches / total
        severity_agreement = severity_matches / total
        summary = {
            "classifier": options["classifier"],
            "dataset": str(dataset),
            "cases": total,
            "category_accuracy": round(category_accuracy, 4),
            "severity_agreement": round(severity_agreement, 4),
            "category_target": options["category_target"],
            "severity_target": options["severity_target"],
            "failures": failures,
        }
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))

        missed = (
            category_accuracy < options["category_target"]
            or severity_agreement < options["severity_target"]
        )
        if options["fail_below_target"] and missed:
            raise CommandError("Classifier missed one or more evaluation targets")
