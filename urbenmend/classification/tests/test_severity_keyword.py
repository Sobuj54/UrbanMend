"""
Classification — the `SeverityKeyword` reference table and its seed (T3.3, data-model §14).

Two subjects, and the second is the one that would fail quietly in production:

  - what the **schema** guarantees (`term` UNIQUE, normalization on every write, the nullable
    category, `PROTECT` on the FK);
  - what the **seed** contains. `bulk_create` bypasses `save()`, so the migration's literals are the
    one place in the project where a term can be stored in a form the matcher can never produce.
    `test_every_seeded_term_is_already_normalized` is named by path in
    `migrations/0003_severity_keyword.py` for exactly that reason.

[doc: data-model §14; PRD FR-13a, FR-14, FR-15, FR-30, NFR-4, NFR-8, NFR-11; Arch §6; C-10;
 Q2 RESOLVED]
"""

from __future__ import annotations

import importlib

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from urbenmend.classification.keywords import normalize_term
from urbenmend.classification.models import (
    Category,
    SeverityKeyword,
    SeverityKeywordStatus,
)
from urbenmend.classification.tests.factories import SeverityKeywordFactory
from urbenmend.reporting.models import SeveritySignal

pytestmark = pytest.mark.django_db

# ⚠️ The seed literals are read from the migration itself rather than restated here.
#
# Restating them would make this suite a copy of the thing it checks — it would pass while the
# applied migration said something else, which is the only failure mode that matters. `import_module`
# because the module name starts with a digit and cannot be written as an `import` statement.
#
# This is the *test* importing a migration, not a migration importing application code — the
# direction `database.md` forbids is the other one.
SEEDED: tuple[tuple[str, str, str, str | None], ...] = importlib.import_module(
    "urbenmend.classification.migrations.0003_severity_keyword"
).SEVERITY_KEYWORDS


# ---------------------------------------------------------------------------------------
# The seed (migration 0003)
# ---------------------------------------------------------------------------------------
def test_the_migration_seeded_every_row() -> None:
    """No `setUp`: the rows come from the migration `pytest-django` already applied. These
    assertions therefore run against the seed a fresh deploy gets, not a fixture."""
    assert SeverityKeyword.objects.count() == len(SEEDED)


def test_every_seeded_term_is_already_normalized() -> None:
    """⚠️ Named by path in the migration's docstring, and the reason the seed can use `bulk_create`.

    `bulk_create` does not call `SeverityKeyword.save()`, so it does not call `normalize_term()`
    either. A literal carrying a capital letter, a double space or decomposed Bangla would sit in
    the table looking perfectly correct and never match a single report — no error, no log line,
    just a rule that silently does nothing for the life of the deployment.
    """
    offenders = [term for term, _lang, _sev, _slug in SEEDED if normalize_term(term) != term]

    assert not offenders, f"Seeded terms are not in match form: {offenders}"


def test_the_stored_rows_match_the_seed_literals() -> None:
    """The above checks the literals; this checks that what reached the database is the same thing
    (a `bulk_create` against a populated table with `ignore_conflicts` could silently skip rows)."""
    assert set(SeverityKeyword.objects.values_list("term", flat=True)) == {
        term for term, _lang, _sev, _slug in SEEDED
    }


def test_seeded_terms_are_unique() -> None:
    """⚠️ `ignore_conflicts=True` in the seed means a duplicated literal would be *dropped* rather
    than raising, so the table would come out one rule short with nothing to show why."""
    terms = [term for term, _lang, _sev, _slug in SEEDED]

    assert len(terms) == len(set(terms))


def test_every_seeded_row_is_active() -> None:
    assert not SeverityKeyword.objects.exclude(status=SeverityKeywordStatus.ACTIVE).exists()


def test_the_seed_is_bilingual() -> None:
    """FR-14 requires the indicators "in **both Bangla and English**" — a seed that shipped English
    only would satisfy every other test here while leaving Bangla reports matching nothing but the
    handful of Latin-script words a citizen happened to mix in."""
    languages = set(SeverityKeyword.objects.values_list("language", flat=True))

    assert languages == {"en", "bn"}


def test_both_languages_cover_every_band() -> None:
    """⚠️ Stronger than "both languages appear", and the gap it closes is real: a seed with Bangla
    only in the Low band would leave a Bangla report describing a live wire triaged as cosmetic."""
    by_language: dict[str, set[str]] = {"en": set(), "bn": set()}
    for _term, language, severity, _slug in SEEDED:
        by_language[language].add(severity)

    bands = {band.value for band in SeveritySignal}
    assert by_language["en"] == bands
    assert by_language["bn"] == bands


def test_every_seeded_category_slug_exists_in_the_taxonomy() -> None:
    """The seed raises on an unknown slug, so this can only fail if the taxonomy changed *after*
    `0003` applied — which is precisely the case the migration cannot catch by itself."""
    slugs = {slug for _term, _lang, _sev, slug in SEEDED if slug is not None}
    known = set(Category.objects.values_list("slug", flat=True))

    assert slugs <= known, f"Seeded keywords name unknown categories: {sorted(slugs - known)}"


def test_the_critical_band_carries_the_documented_life_safety_indicators() -> None:
    """FR-14/Q2 name these explicitly, so they are contract rather than taste. Asserted by term,
    not by count, so the failure message says which indicator went missing."""
    critical = {term for term, _lang, severity, _slug in SEEDED if severity == "critical"}

    assert {"live wire", "gas leak", "collapse"} <= critical


def test_the_documented_high_indicators_are_seeded() -> None:
    high = {term for term, _lang, severity, _slug in SEEDED if severity == "high"}

    assert {"danger", "accident", "flood"} <= high


def test_the_seed_carries_the_companion_row_for_a_dropped_silent_e() -> None:
    """⚠️ The seed's answer to the one inflection prefix matching cannot reach.

    `collapse` is one of FR-14's named indicators and English writes "collapsing", not
    "collapseing" — so the stored term shares only `collaps` with the word a citizen actually types
    and the match fails. The first version of this seed claimed in its own docstring that `collapse`
    covered "collapsing"; it does not, and the consequence landed in the Critical band, where a wall
    reported mid-collapse triaged as Medium with a plausible-looking rationale.

    Asserted here rather than as a general rule over every `e`-final term because the rule is not
    mechanically decidable — "on fire" and "power line" have no `-ing` form, and demanding one would
    fill the table with words nobody writes. `test_keywords.py::
    test_a_dropped_silent_e_is_the_one_inflection_the_matcher_misses` holds the matcher side.
    """
    critical = {term for term, _lang, severity, _slug in SEEDED if severity == "critical"}

    assert {"collapse", "collapsing"} <= critical


def test_no_poi_facility_name_is_a_keyword() -> None:
    """⚠️ C-10: POI/proximity data is display-only and must never affect severity or ordering.

    API §6.5's illustrative rationale reads `"phrases: 'danger','hospital'"`, which makes seeding
    `hospital` look documented — it is not. A facility keyword is proximity data arriving through a
    different door, and it would make the fallback the one classifier that quietly breaks C-10 while
    looking like it was following the spec. `children` is seeded instead: FR-14 names it as *who* is
    at risk, which is a property of the report, not of the neighbourhood.
    """
    terms = {term for term, _lang, _sev, _slug in SEEDED}
    facilities = {"hospital", "school", "clinic", "mosque", "market", "college", "university"}

    assert not (terms & facilities), f"POI facility names seeded as keywords: {terms & facilities}"


def test_no_seeded_term_is_a_single_character() -> None:
    """`MinLengthValidator(2)` never runs against `bulk_create` (validators fire in `full_clean()`),
    so the seed is the one path that could introduce a term matching almost every report."""
    assert all(len(term) >= 2 for term, _lang, _sev, _slug in SEEDED)


def test_the_seed_carries_no_severity_outside_the_four_bands() -> None:
    """⚠️ The column's `choices` are advisory at the database level — Django validates them in
    `full_clean()`, which `bulk_create` skips. A typo'd band would store fine and then raise inside
    `Severity(...)` in `selectors.active_keyword_rules()`, i.e. in the worker, during an outage."""
    bands = {band.value for band in SeveritySignal}

    assert {severity for _term, _lang, severity, _slug in SEEDED} <= bands


def test_a_category_bearing_rule_exists_for_every_band() -> None:
    """FR-13a asks the fallback for a category *and* a severity. A band whose every rule had a
    `NULL` category could raise severity and still leave the report in the `other` sink."""
    with_category = {severity for _term, _lang, severity, slug in SEEDED if slug is not None}

    assert with_category == {band.value for band in SeveritySignal}


# ---------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------
def test_save_normalizes_the_term() -> None:
    """⚠️ In `save()`, not `clean()` — the T1.2 precedent. The stored form must be the match form,
    because `keywords.KeywordFallbackClassifier` compares against `normalize_term(report_text)`."""
    keyword = SeverityKeywordFactory.create(term="  Broken   BENCH  ")

    keyword.refresh_from_db()
    assert keyword.term == "broken bench"


def test_the_term_is_unique() -> None:
    """⚠️ UNIQUE on `term` alone, deliberately not `(term, language)`: matching ignores `language`
    entirely, so two rows sharing a term are indistinguishable to the engine and the second one's
    severity would silently apply to reports in the other language too."""
    SeverityKeywordFactory.create(term="broken bench")

    with pytest.raises(IntegrityError), transaction.atomic():
        SeverityKeywordFactory.create(term="broken bench")


def test_uniqueness_is_enforced_against_the_normalized_form() -> None:
    """The consequence of normalizing in `save()`: `Broken Bench` cannot sneak past the constraint
    by differing only in case, which is what an admin re-typing a rule would produce."""
    SeverityKeywordFactory.create(term="broken bench")

    with pytest.raises(IntegrityError), transaction.atomic():
        SeverityKeywordFactory.create(term="Broken  Bench")


def test_a_seeded_term_cannot_be_re_added() -> None:
    """⚠️ The reason `SeverityKeywordFactory` has no `django_get_or_create`. A test reaching for a
    real rule must *read* it; re-creating one is an `IntegrityError` in whichever test ran first."""
    with pytest.raises(IntegrityError), transaction.atomic():
        SeverityKeywordFactory.create(term="live wire")


def test_the_category_is_optional() -> None:
    """data-model §14's "many → 0..1". `gas leak` raises the band without naming a department, and
    forcing a category on such a phrase would file unrelated reports under whichever node it got."""
    assert SeverityKeyword.objects.get(term="gas leak").category is None


def test_a_rule_can_point_at_a_category() -> None:
    keyword = SeverityKeyword.objects.get(term="live wire")

    assert keyword.category is not None
    assert keyword.category.slug == "electrical"


def test_deleting_a_referenced_category_is_refused() -> None:
    """⚠️ `PROTECT`, not `SET_NULL`. Categories retire rather than delete (data-model §5), so a
    delete that would strand keyword rules is a mistake and should fail loudly — `SET_NULL` would
    quietly turn a categorising rule into a severity-only one and no test would notice."""
    electrical = Category.objects.get(slug="electrical")

    with pytest.raises(ProtectedError), transaction.atomic():
        electrical.delete()


def test_a_one_character_term_is_rejected_by_validation() -> None:
    """⚠️ `MinLengthValidator` runs in `full_clean()`, which the admin form calls and `bulk_create`
    does not. That split is the design: an admin edit is refused while the seeded migration and any
    future bulk load stay unblocked."""
    keyword = SeverityKeyword(term="x", severity=SeveritySignal.LOW)

    with pytest.raises(ValidationError):
        keyword.full_clean()


def test_the_status_lifecycle_is_active_or_retired() -> None:
    """data-model §14 and `database.md` both list Severity Keywords under no-hard-delete. Retiring
    rather than deleting is what keeps FR-15's stored rationales legible: a report explains its
    severity by quoting the phrase that matched, and the rule has to still exist to be read."""
    assert {value for value, _label in SeverityKeywordStatus.choices} == {"active", "retired"}


def test_the_severity_choices_are_the_persisted_bands() -> None:
    """⚠️ Imported from `reporting.SeveritySignal`, never redeclared: a keyword whose band cannot be
    written to a Report is a rule the fallback can match and then fail to persist."""
    field = SeverityKeyword._meta.get_field("severity")

    assert field.choices == SeveritySignal.choices


def test_str_names_the_rule_and_its_band() -> None:
    """Admin changelists and log lines render this; the term alone would not say what it does."""
    assert str(SeverityKeyword.objects.get(term="live wire")) == "live wire → critical"


def test_the_status_index_exists() -> None:
    """The fallback's only read is "every active rule", on every degraded classification (T3.4)."""
    index_names = {index.name for index in SeverityKeyword._meta.indexes}

    assert "classification_kw_status_idx" in index_names
