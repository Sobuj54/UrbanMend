"""
Classification — the provider-facing contract (T3.1).

Two kinds of test here, and the second kind is the point of the file:

  - ordinary behaviour of `highest()`, `parse_severity()`, `coerce_category()` and the two frozen
    dataclasses;
  - **structural** assertions on invariants no type checker can express — that `Severity` still
    matches the persisted enum, that `ClassifierSource` still cannot spell a human decision, and
    that the three classifier modules still import no Django. Each of those is a rule a future
    reader would plausibly "tidy up"; these tests are the reason they cannot.

[doc: Arch §6; PRD FR-10, FR-13a, FR-14, FR-15, NFR-4; plan T3.1; ❓Q2 RESOLVED]
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError

import pytest

from urbenmend.classification.contracts import (
    UNCATEGORIZED_SLUG,
    Classification,
    ClassificationError,
    ClassificationInvalidResponse,
    ClassificationRequest,
    ClassificationUnavailable,
    ClassifierSource,
    Severity,
    coerce_category,
    highest,
    parse_severity,
)
from urbenmend.reporting.models import ClassificationSource, SeveritySignal

ALLOWED = ("roads", "electrical", UNCATEGORIZED_SLUG)


# ---------------------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------------------
def test_severity_matches_the_persisted_enum() -> None:
    """⚠️ The whole justification for declaring `Severity` twice.

    `contracts.Severity` cannot import `reporting.models.SeveritySignal` (a Django import in a
    deliberately Django-free module), so the two are kept in step by this assertion instead. Both
    directions matter: a band added to `SeveritySignal` that `Severity` lacks is a value the
    classifiers can never produce; a band added here that the column lacks is a classification that
    raises on save, inside a Celery task, after the LLM has already been paid for.
    """
    assert [band.value for band in Severity] == [band.value for band in SeveritySignal]


def test_severity_declaration_order_is_most_severe_first() -> None:
    """⚠️ Alphabetizing the members — what an editor's "sort members" action does — silently
    inverts triage, because `highest()` derives its precedence from `list(Severity)`. Asserting the
    *literal* order is the only thing that catches it; every `==` comparison keeps working."""
    assert list(Severity) == [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]


def test_severity_is_a_str_enum_that_renders_as_its_value() -> None:
    """⚠️ Under a `(str, Enum)` mixin this is `"Severity.HIGH"` — a value matching no row and no
    choice, while `==` against `"high"` keeps passing. That combination survives unit tests and
    fails in production, so the rendering is asserted directly."""
    assert str(Severity.HIGH) == "high"
    assert f"{Severity.CRITICAL}" == "critical"


def test_classifier_source_cannot_spell_a_human_decision() -> None:
    """⚠️ `reporting.ClassificationSource` has four members; a classifier may only produce two.

    `citizen` and `authority` record FR-11's *human* corrections. If this enum ever gains one, an
    automated path could claim a person made the call — which is exactly the distinction NFR-9's
    fallback-rate KPI and every "did someone review this?" query rest on.
    """
    assert {source.value for source in ClassifierSource} == {"llm", "fallback"}
    # Both must remain writable to the column, or a classification cannot be persisted at all.
    persisted = {source.value for source in ClassificationSource}
    assert {source.value for source in ClassifierSource} <= persisted
    assert {"citizen", "authority"} <= persisted


@pytest.mark.parametrize("module", ["contracts", "keywords", "llm"])
def test_the_classifier_modules_import_no_django(module: str) -> None:
    """⚠️ T3.1's load-bearing constraint, asserted structurally because nothing else can.

    A classifier that reaches for `settings` or runs a query has bound itself to this deployment,
    and S1's "swap the provider without touching callers" stops being true. The failure mode is
    slow and quiet — one convenient `from django.conf import settings` during a later task — so this
    walks the module's own import statements rather than trusting review.

    `models.py`, `selectors.py` and `services.py` are exempt by design: they are the wiring.
    """
    source = pathlib.Path(__file__).resolve().parent.parent / f"{module}.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name == "django" or name.startswith("django.")]
    assert not offenders, f"{module}.py imports Django: {offenders}"


# ---------------------------------------------------------------------------------------
# highest()
# ---------------------------------------------------------------------------------------
def test_highest_returns_the_most_severe_band() -> None:
    """BR-11's "highest", applied within one report's keyword matches: a body naming both a pothole
    and a live wire is about the live wire."""
    assert highest([Severity.LOW, Severity.CRITICAL, Severity.MEDIUM]) == Severity.CRITICAL


def test_highest_is_order_independent() -> None:
    assert highest([Severity.CRITICAL, Severity.LOW]) == highest([Severity.LOW, Severity.CRITICAL])


def test_highest_accepts_a_single_band() -> None:
    assert highest([Severity.MEDIUM]) == Severity.MEDIUM


def test_highest_consumes_a_generator() -> None:
    """Callers pass a comprehension (see `KeywordFallbackClassifier.classify`); a helper that
    iterated twice would return a `ValueError` on the second pass."""
    assert highest(band for band in (Severity.HIGH, Severity.LOW)) == Severity.HIGH


def test_highest_raises_on_empty() -> None:
    """⚠️ Deliberately not "return LOW on empty". An empty set means the caller matched nothing and
    has a policy decision to make; answering here would make it invisibly, in a helper, and bury
    every unrecognised hazard at the bottom of the queue."""
    with pytest.raises(ValueError, match="at least one severity band"):
        highest([])


# ---------------------------------------------------------------------------------------
# parse_severity()
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("critical", Severity.CRITICAL),
        ("High", Severity.HIGH),
        ("  medium  ", Severity.MEDIUM),
        ("LOW", Severity.LOW),
    ],
)
def test_parse_severity_forgives_case_and_whitespace(raw: str, expected: Severity) -> None:
    """A hosted model asked for `"high"` will sometimes answer `"High"` or `" high"`. Degrading a
    whole classification over capitalisation would spend the LLM's cost and then discard its
    answer."""
    assert parse_severity(raw) == expected


def test_parse_severity_passes_a_band_through() -> None:
    assert parse_severity(Severity.HIGH) is Severity.HIGH


@pytest.mark.parametrize("raw", ["severe", "urgent", "", "critical!", "1"])
def test_parse_severity_rejects_an_unknown_band(raw: str) -> None:
    """⚠️ **No severity sink exists.** PRD §331 names one for categories (`other`) and nothing names
    one for severity, so an out-of-set value can only be rejected — guessing would have this code
    invent a life-safety judgement (FR-14 reserves Critical for life-safety) on the strength of a
    provider typo. The report then goes to the keyword fallback, which decides from evidence."""
    with pytest.raises(ValueError, match="Unknown severity band"):
        parse_severity(raw)


@pytest.mark.parametrize("raw", [None, 3, 1.5, ["high"], {"severity": "high"}])
def test_parse_severity_rejects_a_non_string(raw: object) -> None:
    """A provider returning `{"severity": null}` must not become `Severity("None")`."""
    with pytest.raises(ValueError, match="must be a string"):
        parse_severity(raw)


# ---------------------------------------------------------------------------------------
# coerce_category()
# ---------------------------------------------------------------------------------------
def test_coerce_category_keeps_an_allowed_slug() -> None:
    assert coerce_category("roads", ALLOWED) == "roads"


def test_coerce_category_trims_before_comparing() -> None:
    assert coerce_category("  electrical ", ALLOWED) == "electrical"


@pytest.mark.parametrize("raw", ["potholes", "ROADS", "", None, 7, ["roads"]])
def test_coerce_category_sends_anything_else_to_the_sink(raw: object) -> None:
    """PRD §331: an LLM category outside the allowed set coerces to `Other / Uncategorized`.

    ⚠️ Note `"ROADS"` coerces rather than matching. Slugs are the machine key and are compared
    exactly — a case-insensitive match here would let a provider's capitalisation decide which
    `Category` row a report lands in, and the taxonomy is not case-folded in the database.
    """
    assert coerce_category(raw, ALLOWED) == UNCATEGORIZED_SLUG


def test_coerce_category_never_raises() -> None:
    """⚠️ Coerces, never raises — refusing an off-taxonomy category would fail a classification the
    product can still complete. Severity has no such sink; `parse_severity()` rejects instead."""
    assert coerce_category("nonsense", (UNCATEGORIZED_SLUG,)) == UNCATEGORIZED_SLUG


# ---------------------------------------------------------------------------------------
# ClassificationRequest
# ---------------------------------------------------------------------------------------
def test_a_request_accepts_empty_text() -> None:
    """⚠️ BR-3 allows a photo-only submission, so a report with no description is valid input, not a
    caller bug. Rejecting it here would refuse a whole supported submission shape at the classifier
    boundary."""
    request = ClassificationRequest(text="", allowed_categories=ALLOWED)

    assert request.text == ""


def test_a_request_without_the_sink_is_refused() -> None:
    """⚠️ Fails closed, in the shape T2.1's `active_city_boundary()` established. Without `other`,
    coercion (PRD §331) has nowhere to land and every unrecognised category would be written to the
    Report as a slug no `Category` row matches."""
    with pytest.raises(ValueError, match=UNCATEGORIZED_SLUG):
        ClassificationRequest(text="pothole", allowed_categories=("roads",))


def test_a_request_with_no_categories_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one category"):
        ClassificationRequest(text="pothole", allowed_categories=())


def test_a_request_is_frozen() -> None:
    request = ClassificationRequest(text="pothole", allowed_categories=ALLOWED)

    with pytest.raises(FrozenInstanceError):
        request.text = "something else"  # type: ignore[misc]


def test_a_request_carries_no_identifying_field() -> None:
    """⚠️ P7's PII minimization, enforced structurally rather than by adapter discipline: an adapter
    cannot put an author or a coordinate in a prompt if the request has nowhere to hold one. A new
    field here is a privacy decision, so this test is the conversation's trigger, not a nuisance."""
    assert set(ClassificationRequest.__dataclass_fields__) == {
        "text",
        "allowed_categories",
        "language",
        "image_ref",
    }


# ---------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------
def _classification(
    *,
    category: str = "roads",
    severity: Severity = Severity.MEDIUM,
    confidence: float = 0.7,
    source: ClassifierSource = ClassifierSource.LLM,
    model: str = "test-model/1",
    rationale: str = "",
) -> Classification:
    return Classification(
        category=category,
        severity=severity,
        confidence=confidence,
        source=source,
        model=model,
        rationale=rationale,
    )


def test_a_classification_defaults_to_an_empty_rationale() -> None:
    assert _classification().rationale == ""


@pytest.mark.parametrize("confidence", [-0.1, 1.1, 2.0, 100])
def test_a_classification_rejects_confidence_outside_the_unit_range(confidence: float) -> None:
    """⚠️ Checked on the contract rather than trusted from each implementation: this is the type that
    crosses into `reporting`, where a bad value is not a validation error the API surfaces but an
    NFR-9 KPI that cannot be grouped."""
    with pytest.raises(ValueError, match="0.0–1.0"):
        _classification(confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_a_classification_accepts_the_range_endpoints(confidence: float) -> None:
    assert _classification(confidence=confidence).confidence == confidence


def test_a_classification_requires_a_model() -> None:
    """⚠️ Required even for the keyword engine, which has no "model". The operator question is
    "which code path, in which version, decided this" (FR-10), and an empty string makes NFR-9's
    LLM-vs-fallback comparison impossible to break down."""
    with pytest.raises(ValueError, match="must name the deciding provider"):
        _classification(model="")


def test_a_classification_requires_a_category() -> None:
    with pytest.raises(ValueError, match="non-empty slug"):
        _classification(category="")


def test_a_classification_is_frozen() -> None:
    """⚠️ FR-15: a caller that "adjusts" a confidence or rewrites a rationale after the fact leaves
    the stored explanation describing reasoning that did not produce the stored severity."""
    result = _classification()

    with pytest.raises(FrozenInstanceError):
        result.severity = Severity.CRITICAL  # type: ignore[misc]


# ---------------------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------------------
def test_every_classifier_failure_shares_one_base() -> None:
    """⚠️ T3.4's degradation catches `ClassificationError`, not the leaf types — so a failure mode
    added later degrades to the fallback by default instead of escaping to the worker as an
    unhandled exception. Flattening this hierarchy is what breaks NFR-4."""
    assert issubclass(ClassificationUnavailable, ClassificationError)
    assert issubclass(ClassificationInvalidResponse, ClassificationError)


def test_the_two_failure_types_are_siblings_not_a_chain() -> None:
    """⚠️ `LLMClassificationAdapter` retries `ClassificationUnavailable` and deliberately does not
    retry `ClassificationInvalidResponse`: an unreachable provider may answer in half a second, but
    one returning prose has a prompt or model-version problem that re-asking will repeat, at cost.
    If either type ever became a subclass of the other, that distinction would collapse silently and
    the retry bound would start applying to the wrong failure."""
    assert not issubclass(ClassificationInvalidResponse, ClassificationUnavailable)
    assert not issubclass(ClassificationUnavailable, ClassificationInvalidResponse)
