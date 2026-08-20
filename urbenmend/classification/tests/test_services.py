"""
Classification — the composition roots (T3.1–T3.3, Arch §3.1/§6).

These three factories are the only place settings and database rows become the plain constructor
arguments the Django-free classifiers take, so this file tests the *wiring*: that the caps and
confidences configured in `settings/base.py` actually reach the objects, that the rule set is read
per build rather than pinned for a worker's lifetime, and that a misconfigured provider path fails
loudly instead of looking like an outage.

⚠️ **Nothing here decides which classifier a report gets** — that is T3.4's degradation policy. A
test asserting a fallback happened would be asserting a behaviour this module does not have.

[doc: Arch §3.1, §6; PRD FR-13a, FR-30, NFR-4, NFR-9, NFR-11, NFR-13, P7; plan T3.2, T3.4;
 ❓Q9 RESOLVED-as-deferred, ❓Q10 OPEN]
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from urbenmend.classification.contracts import (
    ClassificationError,
    ClassificationRequest,
    ClassifierSource,
    Severity,
)
from urbenmend.classification.keywords import (
    DEFAULT_SEVERITY,
    FALLBACK_MODEL,
    KeywordFallbackClassifier,
)
from urbenmend.classification.llm import (
    LLMClassificationAdapter,
    LLMCompletion,
    LLMPrompt,
    LLMProvider,
    UnconfiguredLLMProvider,
)
from urbenmend.classification.models import SeverityKeywordStatus
from urbenmend.classification.selectors import active_category_slugs
from urbenmend.classification.services import (
    build_keyword_fallback,
    build_llm_classifier,
    build_llm_provider,
)
from urbenmend.classification.tests.factories import SeverityKeywordFactory
from urbenmend.reporting.models import SeveritySignal

pytestmark = pytest.mark.django_db

_HERE = "urbenmend.classification.tests.test_services"


class RecordingProvider(LLMProvider):
    """A provider `build_llm_provider()` can import by dotted path.

    ⚠️ The recorded prompts are a class attribute because `build_llm_provider()` constructs the class
    with no arguments — which is itself the contract being tested: a provider that needed constructor
    arguments could not be named in a setting, so its configuration would have to leak back into
    `services.py` and the ❓Q9 swap would stop being a one-line change.
    """

    prompts: ClassVar[list[LLMPrompt]] = []

    def complete(self, prompt: LLMPrompt) -> LLMCompletion:
        RecordingProvider.prompts.append(prompt)
        payload = json.dumps(
            {"category": "roads", "severity": "medium", "confidence": 0.6, "rationale": "recorded"}
        )
        return LLMCompletion(text=payload, model="recording/1")


class NotAProvider:
    """Has a `complete()` method and is still not an `LLMProvider` — duck typing is not the check."""

    def complete(self, prompt: LLMPrompt) -> None:
        return None


def not_a_class() -> None:
    """A dotted path can resolve to anything importable, including this."""


@pytest.fixture(autouse=True)
def _clear_recorded_prompts() -> None:
    RecordingProvider.prompts.clear()


def _request(text: str) -> ClassificationRequest:
    """Build a request the way the worker will: from the live taxonomy."""
    return ClassificationRequest(text=text, allowed_categories=active_category_slugs())


# ---------------------------------------------------------------------------------------
# build_keyword_fallback()
# ---------------------------------------------------------------------------------------
def test_the_fallback_is_built_from_the_seeded_rules() -> None:
    """The composition the worker performs, end to end against real reference data: seeded keyword
    rows plus the seeded taxonomy produce a life-safety classification with no LLM involved."""
    result = build_keyword_fallback().classify(_request("a live wire is down over the road"))

    assert isinstance(result.severity, Severity)
    assert result.severity == Severity.CRITICAL
    assert result.category == "electrical"
    assert result.source == ClassifierSource.FALLBACK
    assert result.model == FALLBACK_MODEL


def test_the_fallback_is_the_engine_type_not_a_wrapper() -> None:
    assert isinstance(build_keyword_fallback(), KeywordFallbackClassifier)


def test_a_bangla_report_classifies_through_the_seeded_rules() -> None:
    """FR-14 requires the indicators in both scripts, and this is the assertion that the *seed* and
    the *engine* agree — `test_keywords.py` proves the matcher, `test_severity_keyword.py` proves the
    literals, and only this one proves a Bangla report actually lands on a band."""
    result = build_keyword_fallback().classify(_request("বিদ্যুৎস্পৃষ্ট হয়েছে একজন"))

    assert result.severity == Severity.CRITICAL


@override_settings(
    CLASSIFICATION_FALLBACK_MATCHED_CONFIDENCE=0.77,
    CLASSIFICATION_FALLBACK_UNMATCHED_CONFIDENCE=0.03,
)
def test_the_confidences_come_from_settings() -> None:
    """⚠️ **Settings read at call time, not bound as module constants.** T1.8 learned this the hard
    way with the throttle rates: a value captured at import cannot be reached by `override_settings`,
    so every test of a configured behaviour silently asserts the default and passes. This test only
    means something because the read happens inside the factory."""
    fallback = build_keyword_fallback()

    assert fallback.classify(_request("pothole")).confidence == 0.77
    assert fallback.classify(_request("zzz nothing here zzz")).confidence == 0.03


def test_the_default_band_is_not_configurable() -> None:
    """⚠️ Deliberately absent from settings, unlike the two confidences above. An operator able to set
    the unmatched band would be able to set it to `critical` during an outage — which is the exact
    moment the temptation exists and the moment it would flood the life-safety queue. FR-14 reserves
    Critical for matched life-safety evidence."""
    result = build_keyword_fallback().classify(_request("zzz unrecognised zzz"))

    assert result.severity == DEFAULT_SEVERITY
    assert not hasattr(settings, "CLASSIFICATION_FALLBACK_DEFAULT_SEVERITY")


def test_the_rule_set_is_re_read_on_every_build() -> None:
    """⚠️ FR-30/NFR-11's tuning loop, and the reason the rules are not cached in a module global. The
    worker is a long-lived process: an operator retiring a mis-firing rule mid-incident must see it
    stop matching on the next report, not after a restart nobody can perform during an outage."""
    keyword = SeverityKeywordFactory.create(term="zztestrule", severity=SeveritySignal.CRITICAL)
    request = _request("there is a zztestrule here")

    assert build_keyword_fallback().classify(request).severity == Severity.CRITICAL

    keyword.status = SeverityKeywordStatus.RETIRED
    keyword.save()

    assert build_keyword_fallback().classify(request).severity == DEFAULT_SEVERITY


# ---------------------------------------------------------------------------------------
# build_llm_provider()
# ---------------------------------------------------------------------------------------
def test_the_default_provider_is_the_unconfigured_one() -> None:
    """❓Q9 is resolved as *deferred*, so the shipped default names no vendor. A deployment that sets
    nothing classifies through the FR-13a fallback rather than failing (NFR-4)."""
    assert isinstance(build_llm_provider(), UnconfiguredLLMProvider)


@override_settings(
    CLASSIFICATION_LLM_PROVIDER="openai_compatible",
    CLASSIFICATION_LLM_API_KEY="test-key",
    CLASSIFICATION_LLM_ENDPOINT="https://example.test/v1",
    CLASSIFICATION_LLM_MODEL="test-model",
)
def test_openai_compatible_provider_is_composed_from_settings() -> None:
    from urbenmend.classification.llm import OpenAICompatibleLLMProvider

    provider = build_llm_provider()
    assert isinstance(provider, OpenAICompatibleLLMProvider)
    assert provider.endpoint == "https://example.test/v1"
    assert provider.model == "test-model"


@override_settings(CLASSIFICATION_LLM_PROVIDER="openai_compatible", CLASSIFICATION_LLM_API_KEY="")
def test_openai_compatible_provider_requires_api_key() -> None:
    with pytest.raises(ImproperlyConfigured, match="CLASSIFICATION_LLM_API_KEY"):
        build_llm_provider()


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.RecordingProvider")
def test_a_configured_provider_is_imported_by_path() -> None:
    """S1's "swap the provider without touching callers", as one setting."""
    assert isinstance(build_llm_provider(), RecordingProvider)


@override_settings(CLASSIFICATION_LLM_PROVIDER="urbenmend.classification.llm.NoSuchProvider")
def test_an_unimportable_path_is_a_configuration_error() -> None:
    """⚠️ **`ImproperlyConfigured`, not a silent degrade.** A typo in a dotted path is a deployment
    mistake, categorically different from the provider being down: FR-13a exists so an *outage* does
    not stop triage, and quietly treating a config error as an outage would leave a deployment
    believing it had LLM classification while every report came from the keyword engine — invisible
    in the API, and visible in NFR-9's fallback-rate KPI only as an unexplained step change nobody
    could attribute to anything."""
    with pytest.raises(ImproperlyConfigured, match="CLASSIFICATION_LLM_PROVIDER"):
        build_llm_provider()


@override_settings(CLASSIFICATION_LLM_PROVIDER="not-a-dotted-path")
def test_a_malformed_path_is_a_configuration_error() -> None:
    with pytest.raises(ImproperlyConfigured):
        build_llm_provider()


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.NotAProvider")
def test_a_class_that_is_not_a_provider_is_refused() -> None:
    """⚠️ An explicit `issubclass` check rather than duck typing, and rather than an `assert`: `-O`
    strips asserts, and this guard is what turns "some object with a `complete` method" into the
    contract T3.4 relies on — that every transport failure arrives as a `ClassificationError`."""
    with pytest.raises(ImproperlyConfigured, match="must name a subclass"):
        build_llm_provider()


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.not_a_class")
def test_a_path_naming_a_function_is_refused() -> None:
    """`import_string` resolves anything importable, so "it imported" is not "it is a provider"."""
    with pytest.raises(ImproperlyConfigured, match="must name a subclass"):
        build_llm_provider()


def test_a_configuration_error_is_outside_the_degradable_hierarchy() -> None:
    """⚠️ **The invariant T3.4 must not break.** Its degradation catches `ClassificationError`; if
    `ImproperlyConfigured` were part of that hierarchy — or if T3.4 widened its `except` to
    `Exception` — a config typo would be swallowed by the fallback path and the deployment would never
    learn its provider setting was wrong."""
    assert not issubclass(ImproperlyConfigured, ClassificationError)


# ---------------------------------------------------------------------------------------
# build_llm_classifier()
# ---------------------------------------------------------------------------------------
@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.RecordingProvider")
def test_the_classifier_is_wired_to_the_configured_provider() -> None:
    classifier = build_llm_classifier()

    assert isinstance(classifier, LLMClassificationAdapter)

    result = classifier.classify(_request("a pothole in the road"))

    assert result.source == ClassifierSource.LLM
    assert result.model == "recording/1"
    assert RecordingProvider.prompts


@override_settings(
    CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.RecordingProvider",
    CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS=123,
    CLASSIFICATION_LLM_TIMEOUT_SECONDS=4.5,
)
def test_the_nfr13_caps_reach_the_prompt() -> None:
    """⚠️ Asserted on the prompt the provider actually received, not on the adapter's attributes.

    NFR-13's token ceiling and Arch §6's timeout are applied at composition so no provider can opt out
    of them — a provider that supplied its own would let the *second* provider someone adds quietly
    ignore the cap, and the breach would arrive as an invoice rather than a test failure.
    """
    build_llm_classifier().classify(_request("a pothole in the road"))

    prompt = RecordingProvider.prompts[-1]
    assert prompt.max_output_tokens == 123
    assert prompt.timeout_seconds == 4.5


@override_settings(
    CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.RecordingProvider",
    CLASSIFICATION_LLM_MAX_ATTEMPTS=1,
)
def test_the_retry_bound_comes_from_settings() -> None:
    """NFR-11 — the bound is deployment configuration, not a constant compiled into the adapter."""
    build_llm_classifier().classify(_request("a pothole in the road"))

    assert len(RecordingProvider.prompts) == 1


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.RecordingProvider")
def test_the_prompt_offers_the_live_taxonomy() -> None:
    """The taxonomy reaches the model through the request, so a node added by a migration (NFR-11) is
    offered on the next report with no prompt edit and no deploy."""
    build_llm_classifier().classify(_request("a pothole in the road"))

    prompt = RecordingProvider.prompts[-1]
    for slug in active_category_slugs():
        assert slug in prompt.user


@override_settings(CLASSIFICATION_LLM_PROVIDER=f"{_HERE}.RecordingProvider")
def test_no_report_identifier_reaches_the_provider() -> None:
    """P7 at the outermost seam: whatever the composition adds later, the prompt can still only carry
    what `ClassificationRequest` holds — and it holds no author, id, coordinate or contact."""
    build_llm_classifier().classify(_request("a pothole in the road"))

    prompt = RecordingProvider.prompts[-1]
    assert "a pothole in the road" in prompt.user
    assert not any(character.isdigit() for character in prompt.user)
