"""P3 classification orchestration: controls, degradation, persistence and idempotency."""

from __future__ import annotations

import json
from typing import ClassVar
from unittest.mock import patch

import pytest
from django.test import override_settings

from urbenmend.classification.contracts import ClassificationUnavailable
from urbenmend.classification.keywords import FALLBACK_MODEL
from urbenmend.classification.llm import LLMCompletion, LLMPrompt, LLMProvider
from urbenmend.classification.models import Category
from urbenmend.classification.tasks import classify_report
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.reporting.models import ClassificationSource, ReportStatus, SeveritySignal
from urbenmend.reporting.tests.factories import ClassifiedReportFactory, ReportFactory

pytestmark = pytest.mark.django_db

_HERE = "urbenmend.classification.tests.test_orchestration"


class SuccessfulProvider(LLMProvider):
    calls: ClassVar[int] = 0

    def complete(self, prompt: LLMPrompt) -> LLMCompletion:
        type(self).calls += 1
        return LLMCompletion(
            text=json.dumps(
                {
                    "category": "roads",
                    "severity": "high",
                    "confidence": 0.8,
                    "rationale": "The report says the road is dangerous.",
                }
            ),
            model="successful/1",
            input_tokens=50,
            output_tokens=25,
        )


class UnavailableProvider(LLMProvider):
    calls: ClassVar[int] = 0

    def complete(self, prompt: LLMPrompt) -> LLMCompletion:
        type(self).calls += 1
        raise ClassificationUnavailable("provider unavailable")


class MalformedProvider(LLMProvider):
    calls: ClassVar[int] = 0

    def complete(self, prompt: LLMPrompt) -> LLMCompletion:
        type(self).calls += 1
        return LLMCompletion(
            text='{"category":"roads","severity":"extreme"}',
            model="malformed/1",
        )


@pytest.fixture(autouse=True)
def _reset_provider_calls() -> None:
    SuccessfulProvider.calls = 0
    UnavailableProvider.calls = 0
    MalformedProvider.calls = 0


def test_the_default_unconfigured_provider_falls_back_and_persists() -> None:
    report = ReportFactory.create(description="A live wire is hanging over the road.")

    assert classify_report.run(str(report.pk)) is None

    report.refresh_from_db()
    assert report.status == ReportStatus.TRIAGED
    assert report.issue_id is not None
    assert report.category is not None
    assert report.category.slug == "electrical"
    assert report.severity_signal == SeveritySignal.CRITICAL
    assert report.classification_source == ClassificationSource.FALLBACK
    assert report.classification_model == FALLBACK_MODEL
    assert report.classification_rationale
    assert report.classified_at is not None


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider")
def test_a_successful_llm_result_is_persisted() -> None:
    report = ReportFactory.create(description="A dangerous hole has opened in the road.")

    classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.category is not None
    assert report.category.slug == "roads"
    assert report.severity_signal == SeveritySignal.HIGH
    assert report.confidence == 0.8
    assert report.classification_source == ClassificationSource.LLM
    assert report.classification_model == "successful/1"
    assert report.classification_rationale == "The report says the road is dangerous."
    assert report.status == ReportStatus.TRIAGED
    assert report.issue_id is not None
    assert SuccessfulProvider.calls == 1


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.MalformedProvider")
def test_malformed_llm_output_degrades_to_the_keyword_fallback() -> None:
    report = ReportFactory.create(description="A live wire is sparking beside the road.")

    classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.classification_source == ClassificationSource.FALLBACK
    assert report.severity_signal == SeveritySignal.CRITICAL
    assert MalformedProvider.calls == 1


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider")
def test_repeated_delivery_is_idempotent() -> None:
    report = ReportFactory.create()

    classify_report.run(str(report.pk))
    report.refresh_from_db()
    first_classified_at = report.classified_at
    classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.classified_at == first_classified_at
    assert SuccessfulProvider.calls == 1


def test_two_nearby_worker_runs_cluster_into_one_issue() -> None:
    first = ReportFactory.create(description="A pothole has opened in the road.")
    second = ReportFactory.create(description="A pothole is endangering children in the road.")

    classify_report.run(str(first.pk))
    classify_report.run(str(second.pk))
    first.refresh_from_db()
    second.refresh_from_db()

    assert first.issue_id is not None
    assert second.issue_id == first.issue_id
    assert first.status == second.status == ReportStatus.TRIAGED
    assert first.severity_signal == SeveritySignal.MEDIUM
    assert second.severity_signal == SeveritySignal.HIGH
    assert first.issue is not None
    assert first.issue.computed_severity == SeveritySignal.HIGH
    assert first.issue.computed_severity_rationale == second.classification_rationale


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider")
def test_clustering_failure_retries_without_reclassifying() -> None:
    report = ReportFactory.create(description="A dangerous hole has opened in the road.")

    with (
        patch(
            "urbenmend.issues.services.cluster_report",
            side_effect=RuntimeError("database unavailable"),
        ),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.is_classified is True
    assert report.issue_id is None
    assert report.status == ReportStatus.PROCESSING

    classify_report.run(str(report.pk))
    report.refresh_from_db()

    assert SuccessfulProvider.calls == 1
    assert report.issue_id is not None
    assert report.status == ReportStatus.TRIAGED


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider")
def test_moderated_is_skipped_and_already_classified_is_clustered() -> None:
    moderated = ReportFactory.create(status=ReportStatus.HIDDEN)
    classified = ClassifiedReportFactory.create()

    classify_report.run(str(moderated.pk))
    classify_report.run(str(classified.pk))

    assert SuccessfulProvider.calls == 0
    moderated.refresh_from_db()
    classified.refresh_from_db()
    assert moderated.issue_id is None
    assert classified.issue_id is not None
    assert classified.status == ReportStatus.TRIAGED


def test_an_authority_category_correction_is_not_overwritten() -> None:
    report = ReportFactory.create(
        category=Category.objects.get(slug="roads"),
        classification_source=ClassificationSource.AUTHORITY,
        description="A live wire is down.",
    )

    classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.category is not None
    assert report.category.slug == "roads"
    assert report.classification_source == ClassificationSource.AUTHORITY
    assert report.severity_signal == SeveritySignal.CRITICAL
    assert report.classification_model == FALLBACK_MODEL


@override_settings(CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD=0.2)
def test_a_configured_confidence_threshold_flags_later_review() -> None:
    report = ReportFactory.create(description="No known indicator phrase here.")

    classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.confidence == 0.1
    assert report.classification_needs_review is True


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider")
def test_identical_text_reuses_the_cached_llm_result() -> None:
    first = ReportFactory.create(description="The same damaged road description.")
    second = ReportFactory.create(description="The same damaged road description.")

    classify_report.run(str(first.pk))
    classify_report.run(str(second.pk))

    second.refresh_from_db()
    assert second.classification_source == ClassificationSource.LLM
    assert SuccessfulProvider.calls == 1


@override_settings(
    CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider",
    CLASSIFICATION_LLM_CACHE_SECONDS=0,
    CLASSIFICATION_LLM_USER_RATE_LIMIT=1,
    CLASSIFICATION_LLM_GLOBAL_RATE_LIMIT=100,
)
def test_the_per_user_call_limit_degrades_to_fallback() -> None:
    author = UserFactory.create()
    first = ReportFactory.create(author=author, description="First road defect.")
    second = ReportFactory.create(author=author, description="A live wire is down.")

    classify_report.run(str(first.pk))
    classify_report.run(str(second.pk))

    second.refresh_from_db()
    assert second.classification_source == ClassificationSource.FALLBACK
    assert SuccessfulProvider.calls == 1


@override_settings(
    CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.SuccessfulProvider",
    CLASSIFICATION_LLM_CACHE_SECONDS=0,
    CLASSIFICATION_LLM_DAILY_TOKEN_BUDGET=1,
)
def test_the_spend_guard_degrades_before_calling_the_provider() -> None:
    report = ReportFactory.create(description="A live wire is down.")

    classify_report.run(str(report.pk))

    report.refresh_from_db()
    assert report.classification_source == ClassificationSource.FALLBACK
    assert SuccessfulProvider.calls == 0


@override_settings(
    CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.UnavailableProvider",
    CLASSIFICATION_LLM_CACHE_SECONDS=0,
    CLASSIFICATION_LLM_MAX_ATTEMPTS=1,
    CLASSIFICATION_LLM_CIRCUIT_FAILURE_THRESHOLD=1,
)
def test_the_circuit_opens_after_the_configured_failure_threshold() -> None:
    first = ReportFactory.create(description="First unknown issue.")
    second = ReportFactory.create(description="Second unknown issue.")

    classify_report.run(str(first.pk))
    classify_report.run(str(second.pk))

    second.refresh_from_db()
    assert second.classification_source == ClassificationSource.FALLBACK
    assert UnavailableProvider.calls == 1
