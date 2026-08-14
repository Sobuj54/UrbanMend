"""
Classification — the reference-data reads (T3.3, Arch §3.1).

This module is the only bridge between the two reference tables and the Django-free classifiers, so
the tests here are about *what crosses it*: plain values in the shapes `ClassificationRequest` and
`KeywordFallbackClassifier` take, with retired rows already filtered out. A bug here is invisible
inside the classifiers — they would faithfully classify against a rule set that should not exist.

[doc: Arch §3.1, §6; PRD FR-13a, FR-30, NFR-11; data-model §5, §14; PRD §331]
"""

from __future__ import annotations

import pytest

from urbenmend.classification.contracts import (
    UNCATEGORIZED_SLUG,
    ClassificationRequest,
    Severity,
)
from urbenmend.classification.keywords import KeywordRule, normalize_term
from urbenmend.classification.models import (
    Category,
    CategoryStatus,
    SeverityKeyword,
    SeverityKeywordStatus,
)
from urbenmend.classification.selectors import active_category_slugs, active_keyword_rules
from urbenmend.classification.tests.factories import SeverityKeywordFactory
from urbenmend.reporting.models import SeveritySignal

pytestmark = pytest.mark.django_db


def _retire(slug: str) -> None:
    """Retire a seeded category without going through the read-only admin."""
    Category.objects.filter(slug=slug).update(status=CategoryStatus.RETIRED)


# ---------------------------------------------------------------------------------------
# active_category_slugs()
# ---------------------------------------------------------------------------------------
def test_the_seeded_taxonomy_is_returned() -> None:
    """The seven nodes `classification/0001` seeds (PRD §6.2), which are what a real classification
    is offered — not a fixture invented here (C-2: the taxonomy is controlled)."""
    assert set(active_category_slugs()) == set(
        Category.objects.filter(status=CategoryStatus.ACTIVE).values_list("slug", flat=True)
    )


def test_the_sink_is_always_present() -> None:
    """PRD §331 — `other` is where off-taxonomy output lands, so it must be offered every time."""
    assert UNCATEGORIZED_SLUG in active_category_slugs()


def test_the_result_is_ordered_and_hashable_as_a_tuple() -> None:
    """⚠️ A `tuple`, because `ClassificationRequest` is frozen and hashable — a list would make the
    request unhashable and quietly rule out T3.4's identical-text cache (NFR-13). Sorted so the LLM
    prompt's category list is byte-identical between calls, which is what makes a prompt cache hit."""
    slugs = active_category_slugs()

    assert isinstance(slugs, tuple)
    assert list(slugs) == sorted(slugs)


def test_the_result_builds_a_request_directly() -> None:
    """The contract this selector exists to satisfy: no conversion step at the call site, so nobody
    is tempted to add one that drops the sink."""
    request = ClassificationRequest(text="pothole", allowed_categories=active_category_slugs())

    assert request.allowed_categories == active_category_slugs()


def test_a_retired_category_is_not_offered() -> None:
    """data-model §5 — a retired node keeps its history but accepts no new classification. This is
    the mechanism: it is never offered, so no classifier can pick it."""
    _retire("roads")

    assert "roads" not in active_category_slugs()


def test_a_missing_sink_raises_rather_than_returning_the_rest() -> None:
    """⚠️ Fails closed, in the shape T2.1's `active_city_boundary()` established.

    Returning the remaining six would push the failure into `ClassificationRequest.__post_init__` —
    the same outcome, reported from a dataclass three frames from the reference-data mistake that
    caused it. It also means nobody can write `if UNCATEGORIZED_SLUG in slugs:` and treat the absence
    as a soft condition.
    """
    _retire(UNCATEGORIZED_SLUG)

    with pytest.raises(RuntimeError, match=UNCATEGORIZED_SLUG):
        active_category_slugs()


# ---------------------------------------------------------------------------------------
# active_keyword_rules()
# ---------------------------------------------------------------------------------------
def test_the_rules_are_plain_value_objects() -> None:
    """⚠️ `KeywordRule`, not `SeverityKeyword`. Handing model instances to the engine is what would
    make `keywords.py` Django-dependent — and would let a lazy FK attribute issue a query per rule,
    inside the classifier, on every degraded classification."""
    rules = active_keyword_rules()

    assert rules
    assert all(isinstance(rule, KeywordRule) for rule in rules)


def test_the_severity_arrives_as_the_enum_not_a_string() -> None:
    """The column stores `SeveritySignal`'s values; the engine's `_ranked()` indexes into
    `list(Severity)`. Widening at this boundary is why that lookup cannot raise."""
    rules = active_keyword_rules()

    assert all(isinstance(rule.severity, Severity) for rule in rules)


def test_every_seeded_rule_is_loaded() -> None:
    assert len(active_keyword_rules()) == SeverityKeyword.objects.count()


def test_the_loaded_terms_are_in_match_form() -> None:
    """The stored form is the match form (`SeverityKeyword.save()`), and nothing here should undo
    that — the engine re-normalizes anyway, so a discrepancy would hide rather than break."""
    assert all(normalize_term(rule.term) == rule.term for rule in active_keyword_rules())


def test_a_rule_carries_its_category_slug() -> None:
    """The slug, not the FK id: it is what `allowed_categories` is expressed in."""
    by_term = {rule.term: rule for rule in active_keyword_rules()}

    assert by_term["live wire"].category == "electrical"
    assert by_term["live wire"].severity == Severity.CRITICAL


def test_a_rule_with_no_category_keeps_a_none() -> None:
    """⚠️ `None`, never `""`. `_category_for()` tests `if rule.category and ...`, so both are falsy
    today — but `""` would compare equal to nothing in the taxonomy while *looking* like a slug, and
    the first reader to write `rule.category in allowed` gets a rule that silently never categorises.
    """
    by_term = {rule.term: rule for rule in active_keyword_rules()}

    assert by_term["gas leak"].category is None


def test_a_retired_rule_is_excluded() -> None:
    """FR-30's tuning loop: an operator retiring a mis-firing rule mid-incident must see it stop
    matching on the next report, not after a worker restart."""
    keyword = SeverityKeywordFactory.create(
        term="zzretired", severity=SeveritySignal.CRITICAL, status=SeverityKeywordStatus.RETIRED
    )

    assert keyword.term not in {rule.term for rule in active_keyword_rules()}


def test_an_active_rule_is_included() -> None:
    keyword = SeverityKeywordFactory.create(term="zzactive", severity=SeveritySignal.HIGH)

    assert keyword.term in {rule.term for rule in active_keyword_rules()}


def test_a_rule_pointing_at_a_retired_category_is_excluded() -> None:
    """⚠️ The second filter, and the easy one to forget. A rule whose category has been retired would
    otherwise send reports to a node that accepts no new classification (data-model §5) — the engine
    would skip it as not-allowed, but only because `active_category_slugs()` no longer offers it, so
    the rule's *severity* would still apply from a node nobody can act on."""
    SeverityKeywordFactory.create(
        term="zzretiredcategory",
        severity=SeveritySignal.HIGH,
        category=Category.objects.get(slug="roads"),
    )
    _retire("roads")

    assert "zzretiredcategory" not in {rule.term for rule in active_keyword_rules()}


def test_a_severity_only_rule_survives_the_category_filter() -> None:
    """⚠️ The reason the filter is `Q(category__isnull=True) | Q(category__status=ACTIVE)` and not a
    bare `category__status=ACTIVE`. The latter is an inner join: it would silently drop every
    severity-only rule — `gas leak`, `injured`, `explosion` — i.e. most of the life-safety set."""
    _retire("roads")
    terms = {rule.term for rule in active_keyword_rules()}

    assert "gas leak" in terms


def test_the_language_is_carried_through() -> None:
    """Recorded for the admin and the logs, never consulted for matching (`KeywordRule.language`)."""
    languages = {rule.language for rule in active_keyword_rules()}

    assert languages == {"en", "bn"}


def test_the_result_is_a_tuple() -> None:
    """Immutable in the shape the engine takes it: `KeywordFallbackClassifier` compiles once per
    build, so a caller mutating the sequence afterwards would change nothing and look like it had."""
    assert isinstance(active_keyword_rules(), tuple)


def test_an_empty_rule_table_is_not_an_error() -> None:
    """⚠️ Deliberately unlike `active_category_slugs()`. A missing taxonomy sink makes classification
    impossible; an empty keyword table just means nothing matches, and FR-13a still answers (the
    engine returns the sink at the default band). Raising here would turn a survivable state into an
    outage during exactly the incident the fallback exists for."""
    SeverityKeyword.objects.update(status=SeverityKeywordStatus.RETIRED)

    assert active_keyword_rules() == ()
