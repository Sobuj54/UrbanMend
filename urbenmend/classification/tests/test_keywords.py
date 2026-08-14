"""
Classification — the deterministic keyword fallback engine (T3.3, FR-13a).

Pure unit tests: no database, no settings, no migration. That is `keywords.py`'s Django-free
constraint paying for itself — every rule below is stated as a one-line tuple.

The suite is organised around what FR-13a actually promises: **an answer, always**, the same answer
every time, from admin-managed bilingual phrases. The negative tests matter more than the positive
ones here, because this classifier is the one that runs when the other one is broken.

[doc: Arch §6 "Keyword Fallback Engine"; PRD FR-12, FR-13a, FR-14, FR-15, FR-30, NFR-4, NFR-13,
 RISK-5, RISK-12; ❓Q2 RESOLVED]
"""

from __future__ import annotations

import pytest

from urbenmend.classification.contracts import (
    UNCATEGORIZED_SLUG,
    Classification,
    ClassificationRequest,
    ClassifierSource,
    Severity,
)
from urbenmend.classification.keywords import (
    DEFAULT_MATCHED_CONFIDENCE,
    DEFAULT_SEVERITY,
    DEFAULT_UNMATCHED_CONFIDENCE,
    FALLBACK_MODEL,
    MAX_RATIONALE_TERMS,
    KeywordFallbackClassifier,
    KeywordRule,
    normalize_term,
)

ALLOWED = (
    "roads",
    "street_lighting",
    "water_drainage",
    "electrical",
    "public_structures",
    UNCATEGORIZED_SLUG,
)

RULES = (
    KeywordRule(term="live wire", severity=Severity.CRITICAL, category="electrical", language="en"),
    KeywordRule(term="gas leak", severity=Severity.CRITICAL, language="en"),
    KeywordRule(term="collapse", severity=Severity.CRITICAL, category="public_structures"),
    KeywordRule(term="children", severity=Severity.HIGH, language="en"),
    KeywordRule(term="flood", severity=Severity.HIGH, category="water_drainage", language="en"),
    KeywordRule(term="pothole", severity=Severity.MEDIUM, category="roads", language="en"),
    KeywordRule(term="drain", severity=Severity.LOW, category="water_drainage", language="en"),
    KeywordRule(term="বিদ্যুৎ", severity=Severity.LOW, category="electrical", language="bn"),
    KeywordRule(term="বন্যা", severity=Severity.HIGH, category="water_drainage", language="bn"),
)


def classify(
    text: str, *, rules: tuple[KeywordRule, ...] = RULES, language: str = "en"
) -> Classification:
    """Run one report through a classifier built from `rules`."""
    classifier = KeywordFallbackClassifier(rules)
    return classifier.classify(
        ClassificationRequest(text=text, allowed_categories=ALLOWED, language=language)
    )


# ---------------------------------------------------------------------------------------
# normalize_term — one function on both sides of the comparison
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Live Wire", "live wire"),
        ("  gas   leak  ", "gas leak"),
        ("GAS\nLEAK", "gas leak"),
        ("pothole", "pothole"),
        ("বিদ্যুৎ", "বিদ্যুৎ"),
    ],
)
def test_normalize_term_folds_case_and_collapses_whitespace(raw: str, expected: str) -> None:
    assert normalize_term(raw) == expected


def test_normalize_term_is_idempotent() -> None:
    """⚠️ Load-bearing: the term is normalized on save *and* again when the engine loads it, so a
    second pass must not change the value. A non-idempotent normalizer would make a stored term
    unmatchable the moment it round-tripped."""
    for raw in ("Live Wire", "  বিদ্যুৎ ", "GAS\tLEAK"):
        once = normalize_term(raw)
        assert normalize_term(once) == once


def test_normalize_term_reconciles_composed_and_decomposed_bangla() -> None:
    """⚠️ NFC is not decoration. Bangla text arrives composed or decomposed depending on the
    keyboard; the two render identically and compare unequal, so a term stored one way would never
    match a report typed the other and nothing in the admin would reveal why.

    The pair below is `ড়` written as one codepoint and as `ড` + nukta. Which of the two NFC settles
    on is Unicode's business (U+09DC is a composition exclusion, so it is in fact the decomposed
    form) — what this asserts is only that both spellings reach the *same* stored term.
    """
    composed = chr(0x09DC)  # BENGALI LETTER RRA, a single codepoint
    decomposed = chr(0x09A1) + chr(0x09BC)  # BENGALI LETTER DDA + NUKTA, same grapheme

    assert composed != decomposed  # identical on screen, unequal as strings
    assert normalize_term(composed) == normalize_term(decomposed)


# ---------------------------------------------------------------------------------------
# FR-13a: an answer, always
# ---------------------------------------------------------------------------------------
def test_an_unmatched_report_still_gets_a_category_and_a_severity() -> None:
    """FR-13a's whole promise. ⚠️ This is a *result*, not an error — `classify()` has no failure
    mode, because the path that exists to answer when the other cannot must not itself be able to
    fail."""
    result = classify("something nobody thought to seed a keyword for")

    assert result.category == UNCATEGORIZED_SLUG
    assert result.severity == DEFAULT_SEVERITY
    assert result.confidence == DEFAULT_UNMATCHED_CONFIDENCE
    assert result.source == ClassifierSource.FALLBACK
    assert result.model == FALLBACK_MODEL


def test_the_unmatched_default_is_medium_not_low() -> None:
    """⚠️ An unmatched report is *unknown*, not unimportant, and this engine only runs while the LLM
    is unavailable — the bottom band would systematically bury everything the keyword list happens
    not to cover, for the whole duration of an outage."""
    assert DEFAULT_SEVERITY == Severity.MEDIUM


def test_no_default_can_reach_critical() -> None:
    """⚠️ Q2/FR-14 reserve Critical for life-safety, so it must be *matched*, never assumed. A
    default of Critical would fill the life-safety queue with everything the list missed."""
    assert classify("").severity != Severity.CRITICAL
    assert classify("nothing recognisable here").severity != Severity.CRITICAL


def test_an_empty_report_is_answered_not_refused() -> None:
    """BR-3 allows a photo-only submission, so empty text reaches a text-only classifier as a matter
    of course."""
    result = classify("")

    assert result.category == UNCATEGORIZED_SLUG
    assert result.rationale


def test_an_empty_rule_set_still_answers() -> None:
    """A deployment whose keyword table has not been seeded, or whose rules were all retired mid
    incident, must still classify — NFR-4 does not have an exception for that."""
    result = classify("live wire down on the road", rules=())

    assert result.category == UNCATEGORIZED_SLUG
    assert result.severity == DEFAULT_SEVERITY


# ---------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------
def test_a_matched_term_sets_the_band_and_the_category() -> None:
    result = classify("there is a live wire hanging over the footpath")

    assert result.severity == Severity.CRITICAL
    assert result.category == "electrical"
    assert result.confidence == DEFAULT_MATCHED_CONFIDENCE
    assert result.source == ClassifierSource.FALLBACK


def test_matching_ignores_the_case_of_the_report() -> None:
    assert classify("LIVE WIRE").severity == Severity.CRITICAL


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the wall collapsed", Severity.CRITICAL),
        ("the wall collapses a little every day", Severity.CRITICAL),
        ("wall collapse", Severity.CRITICAL),
        ("the road is flooding", Severity.HIGH),
        ("the road flooded last night", Severity.HIGH),
        ("potholes everywhere", Severity.MEDIUM),
    ],
)
def test_a_term_matches_its_english_inflections(text: str, expected: Severity) -> None:
    """⚠️ The reason the pattern is open at the end. With a trailing `\\b`, `collapse` would miss
    "collapsed" and every unseeded inflection would silently under-triage."""
    assert classify(text).severity == expected


def test_a_dropped_silent_e_is_the_one_inflection_the_matcher_misses() -> None:
    """⚠️ **The limit of prefix matching, asserted rather than assumed.** English drops the silent `e`
    before `-ing`, so "collapsing" shares only `collaps` with `collapse` and no anchor choice can
    bridge that — the alternatives are a shorter stem (`collaps`, which also fires on "collapsible")
    or a second row.

    This is asserted from the *failing* side because that is the only side that is visible: the report
    still classifies, one band too low, quoting whatever weaker rule did match — no error, no log
    line. A reader who deletes this test because "it looks like a bug" needs to see that the seed's
    answer is `test_the_seed_carries_the_companion_row_for_a_dropped_silent_e` in
    `test_severity_keyword.py`, not a change to `_compile`.
    """
    assert classify("the wall is collapsing").severity == DEFAULT_SEVERITY

    # And the demonstration that a companion row — not a regex change — is what fixes it.
    with_companion = (*RULES, KeywordRule(term="collapsing", severity=Severity.CRITICAL))
    assert classify("the wall is collapsing", rules=with_companion).severity == Severity.CRITICAL


def test_a_term_does_not_match_inside_a_word() -> None:
    """⚠️ The reason the pattern is anchored at the *start* of a word. Without the lookbehind,
    `drain` would fire on "eardrain"-style substrings and, worse, short stems would match half the
    dictionary."""
    result = classify("the aftercollapse report mentions nothing else")

    assert result.severity == DEFAULT_SEVERITY


def test_a_bangla_term_matches_its_suffixed_form() -> None:
    """Bangla attaches case and postposition endings directly to the stem, so the same open-ended
    pattern that covers English suffixes covers these."""
    assert classify("বন্যার পানি ঘরে ঢুকেছে").severity == Severity.HIGH


def test_an_english_indicator_matches_a_report_marked_bangla() -> None:
    """⚠️ FR-12/A1 make code-mixed "Banglish" a first-class input, so rules are matched regardless
    of `language`. Filtering rules by the request's language would miss exactly the case bilingual
    understanding exists for."""
    result = classify("রাস্তায় live wire পড়ে আছে", language="bn")

    assert result.severity == Severity.CRITICAL
    assert result.category == "electrical"


def test_the_highest_matched_band_wins() -> None:
    """BR-11's "highest", within one report: a body naming both a pothole and a live wire is about
    the live wire."""
    result = classify("a pothole near the drain, and a live wire above it")

    assert result.severity == Severity.CRITICAL


def test_a_blank_term_is_dropped_rather_than_compiled() -> None:
    """⚠️ `_compile("")` matches at every position, so one empty row would mark every report as
    matched at whatever severity it carried — a single bad admin edit silently re-triaging the whole
    city."""
    rules = (
        KeywordRule(term="  ", severity=Severity.CRITICAL, category="electrical"),
        KeywordRule(term="pothole", severity=Severity.MEDIUM, category="roads"),
    )

    assert classify("nothing here", rules=rules).severity == DEFAULT_SEVERITY
    assert classify("a pothole", rules=rules).severity == Severity.MEDIUM


def test_a_rule_written_unnormalized_still_matches() -> None:
    """The engine normalizes what it is handed, so a hand-written rule (or a row inserted by a path
    that bypassed `save()`) behaves the same as a stored one."""
    rules = (KeywordRule(term="Gas  Leak", severity=Severity.CRITICAL),)

    assert classify("smell of gas leak in the lane", rules=rules).severity == Severity.CRITICAL


def test_a_regex_metacharacter_in_a_term_is_matched_literally() -> None:
    """An admin typing `wire (exposed)` must get a keyword, not a regex — and not a crash."""
    rules = (KeywordRule(term="wire (exposed)", severity=Severity.CRITICAL, category="electrical"),)

    assert classify("a wire (exposed) near the school", rules=rules).severity == Severity.CRITICAL
    assert classify("wire exposed", rules=rules).severity == DEFAULT_SEVERITY


# ---------------------------------------------------------------------------------------
# Determinism and category selection
# ---------------------------------------------------------------------------------------
def test_the_answer_does_not_depend_on_rule_order() -> None:
    """⚠️ FR-13a says "deterministic", and `_ranked` is a *total* order so that is literally true.
    Sorting by severity alone would leave ties resolved by whatever order the queryset returned, so
    the same report could land in `roads` today and `public_structures` after an index change."""
    forward = classify("pothole beside the drain")
    reversed_rules = tuple(reversed(RULES))
    backward = classify("pothole beside the drain", rules=reversed_rules)

    assert (forward.category, forward.severity) == (backward.category, backward.severity)
    assert forward.rationale == backward.rationale


def test_the_more_severe_match_decides_the_category() -> None:
    result = classify("pothole full of flood water")

    assert result.severity == Severity.HIGH
    assert result.category == "water_drainage"


def test_the_longer_term_decides_the_category_within_one_band() -> None:
    """Specificity tie-break: `gas leak` should decide over `leak`."""
    rules = (
        KeywordRule(term="leak", severity=Severity.CRITICAL, category="water_drainage"),
        KeywordRule(term="gas leak", severity=Severity.CRITICAL, category="electrical"),
    )

    assert classify("a gas leak in the kitchen", rules=rules).category == "electrical"


def test_a_severity_only_rule_falls_through_to_the_next_category() -> None:
    """⚠️ data-model §14's "many → 0..1": `gas leak` raises the band without saying which department
    owns it, so the category comes from the next-ranked match rather than the sink."""
    result = classify("gas leak near a pothole")

    assert result.severity == Severity.CRITICAL
    assert result.category == "roads"


def test_a_severity_only_match_alone_lands_in_the_sink() -> None:
    result = classify("children playing here")

    assert result.severity == Severity.HIGH
    assert result.category == UNCATEGORIZED_SLUG


def test_a_rule_pointing_at_a_disallowed_category_is_skipped_not_coerced() -> None:
    """⚠️ A keyword can point at a node that has since been retired (categories retire, never
    delete). Coercing on that first candidate would drop a report into `other` while a perfectly
    good second match named a live node."""
    rules = (
        KeywordRule(term="live wire", severity=Severity.CRITICAL, category="retired-node"),
        KeywordRule(term="pothole", severity=Severity.MEDIUM, category="roads"),
    )
    result = classify("live wire over a pothole", rules=rules)

    assert result.severity == Severity.CRITICAL
    assert result.category == "roads"


def test_the_same_input_classifies_identically_on_repeat() -> None:
    first = classify("live wire and children nearby")
    second = classify("live wire and children nearby")

    assert first == second


# ---------------------------------------------------------------------------------------
# FR-15: explainability
# ---------------------------------------------------------------------------------------
def test_the_rationale_names_the_matched_phrases_and_their_bands() -> None:
    """FR-14's acceptance criterion is "flagged High: 'live wire', 'children'" and API §6.5 shows
    `"phrases: 'danger','hospital'"` — a human-readable string naming the phrases, not a score."""
    rationale = classify("live wire where children play").rationale

    assert "live wire" in rationale
    assert "children" in rationale
    assert "critical" in rationale


def test_the_rationale_leads_with_the_deciding_phrase() -> None:
    """⚠️ FR-15 is about *explaining the band*, so the phrase that set it must be the one an
    Authority reads first — a rationale that opened with the pothole would explain the wrong thing."""
    rationale = classify("pothole, drain, and a live wire").rationale

    assert rationale.index("live wire") < rationale.index("pothole")


def test_the_rationale_is_bounded() -> None:
    """A report stuffed with indicators must not produce a paragraph in the Authority UI."""
    rules = tuple(KeywordRule(term=f"hazard{index}", severity=Severity.HIGH) for index in range(12))
    text = " ".join(f"hazard{index}" for index in range(12))

    rationale = classify(text, rules=rules).rationale

    assert rationale.count("→") == MAX_RATIONALE_TERMS
    assert "more)" in rationale


def test_a_duplicate_term_is_listed_once() -> None:
    """⚠️ Two rules can normalize to the same term (different languages, say); listing a phrase twice
    reads as corroboration the engine never found."""
    rules = (
        KeywordRule(term="flood", severity=Severity.HIGH, category="water_drainage", language="en"),
        KeywordRule(term="Flood", severity=Severity.HIGH, category="water_drainage", language="bn"),
    )

    assert classify("flood everywhere", rules=rules).rationale.count("flood") == 1


def test_the_unmatched_rationale_says_it_defaulted() -> None:
    """An Authority reading a Medium band must be able to tell "a keyword said Medium" from
    "nothing matched, so we said Medium" — the difference decides whether to look again."""
    rationale = classify("entirely unrecognised text").rationale

    assert "no severity keyword matched" in rationale
    assert str(DEFAULT_SEVERITY) in rationale


# ---------------------------------------------------------------------------------------
# NFR-9: the recorded model
# ---------------------------------------------------------------------------------------
def test_the_fallback_names_itself_as_the_deciding_engine() -> None:
    """FR-10 wants "the model/provider + version used"; there is no model here, so the rule engine
    and its version stand in — otherwise NFR-9 cannot group outcomes by what decided them."""
    assert classify("pothole").model == FALLBACK_MODEL


def test_the_fallback_model_is_stable_across_keyword_edits() -> None:
    """⚠️ It must not embed the rule count or a data checksum: NFR-9's KPI groups by this value, and
    a value that changes every time an admin adds a keyword (FR-30) turns one series into
    hundreds."""
    few = KeywordFallbackClassifier(RULES[:2])
    many = KeywordFallbackClassifier(RULES)
    request = ClassificationRequest(text="live wire", allowed_categories=ALLOWED)

    assert few.classify(request).model == many.classify(request).model
