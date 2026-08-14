"""
Classification — the deterministic keyword fallback engine (T3.3, FR-13a).

The classifier that runs when the hosted LLM is unavailable, timed out, or over the NFR-13 cap.
NFR-4 makes this the reason the product "never hard-depends on the external API": every report
still gets a category and a severity, so nothing is left untriaged.

⚠️ **No Django imports here either** — this is the engine, not its wiring. It is handed a list of
`KeywordRule`s and asked a question; `selectors.active_keyword_rules()` reads the
`SeverityKeyword` table and `services.build_keyword_fallback()` composes the two. That split is
what lets the whole matching contract — inflection, code-mixed text, precedence, the rationale —
be tested with plain tuples and no migration [doc: plan T3.1 "no Django imports", Arch §6].

⚠️ **`classify()` has no failure mode, deliberately.** It raises no `ClassificationError` and
catches nothing, because FR-13a's guarantee is circular otherwise: the path that exists to answer
when the other path cannot must not itself be able to fail. Every branch below ends in a
`Classification` — including "no rule matched anything", which is a *result*, not an error.

[doc: Arch §6 "Keyword Fallback Engine"; PRD FR-12, FR-13a, FR-14, FR-15, FR-30, NFR-4, NFR-13,
 RISK-5, RISK-12; data-model §14]
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace

from urbenmend.classification.contracts import (
    UNCATEGORIZED_SLUG,
    Classification,
    ClassificationRequest,
    ClassificationService,
    ClassifierSource,
    Severity,
    coerce_category,
    highest,
)

# FR-10 wants "the model/provider + version used"; there is no model here, so this records the
# deciding *rule engine* and its version instead.
#
# ⚠️ **Stable across keyword edits.** It must not embed the rule count or a data checksum: NFR-9's
# KPI groups classification outcomes by this value to compare LLM and fallback accuracy, and a
# value that changes every time an admin adds a keyword (FR-30) turns one series into hundreds.
# Bump it when the *matching behaviour* changes — that is a genuinely different classifier.
FALLBACK_MODEL = "keyword-fallback/1"

# ⚠️ **Our numbers, not spec-derived**, and kept overridable from settings (NFR-11) — the same
# resolution T1.2 took for the verification-code policy and T1.8 for the throttle rates. ❓Q10 (the
# accuracy bar / low-confidence threshold) is **open**, so these are deliberately *not* tuned
# against a threshold that does not exist yet.
#
# A keyword hit is real evidence and no understanding of context, so 0.5 sits exactly at the
# midpoint: any Q10 threshold above it flags every fallback classification for human review
# (T3.7), which is the conservative default while the LLM is the thing that is down.
DEFAULT_MATCHED_CONFIDENCE = 0.5
# Nothing matched: the category is the sink and the severity is a policy default, so there is
# nothing to be confident about. Kept strictly below the matched value, and off `0.0` — readers
# and dashboards routinely treat a hard zero as "field not set" rather than "measured floor".
DEFAULT_UNMATCHED_CONFIDENCE = 0.1

# ⚠️ **`MEDIUM`, not `LOW`, and this is a judgement worth defending.** An unmatched report is
# *unknown*, not unimportant, and this engine only runs while the LLM is unavailable — so choosing
# the bottom band would systematically bury everything the keyword list happens not to cover, for
# the whole duration of an outage. `MEDIUM` states "we did not recognise this" without asserting a
# judgement the engine never made. Q2 also puts Critical out of reach of any default: it is
# reserved for life-safety (FR-14) and must be *matched*, never assumed.
DEFAULT_SEVERITY = Severity.MEDIUM

# FR-15 wants the *key* phrases, and a report stuffed with indicators should not produce a
# paragraph. Bounded here rather than at the column (it is a `TextField`) so the rationale stays
# readable in the Authority UI.
MAX_RATIONALE_TERMS = 5


def normalize_term(value: str) -> str:
    """Fold a keyword or a report body to the single form matching happens in.

    ⚠️ **One function for both sides of the comparison, and that is the point.** `SeverityKeyword`
    stores what this returns (its `save()` calls it) and the engine feeds report text through the
    same call, so a stored term can always match. Two normalizers — even two that agree today —
    would let a keyword be stored in a form the matcher can never produce, and the failure is
    silent: the keyword simply never fires.

    Three steps, each load-bearing:

      - **NFC** — Bangla text arrives in either composed or decomposed form depending on the
        keyboard that typed it, and the two are different byte strings that render identically. A
        term stored NFC would never match a report typed NFD, and no amount of staring at the
        admin would reveal why.
      - **casefold**, not `lower()` — the Unicode-correct fold, and a no-op for Bengali script,
        which has no case. It is what makes `Live Wire` in the admin match `live wire` in a report.
      - **whitespace collapse** — a multi-word term (`gas leak`) pasted with a double space, or a
        report with a newline between the words, must still match.
    """
    folded = unicodedata.normalize("NFC", value).casefold()
    return " ".join(folded.split())


def _compile(term: str) -> re.Pattern[str]:
    """Build the matcher for one normalized term.

    ⚠️ **Anchored at the start of a word and open at the end** — `(?<!\\w)term`, deliberately not
    `\\bterm\\b`. One rule has to serve both scripts:

      - English inflects by suffix: a trailing anchor makes `collapse` miss "collapsed", `flood`
        miss "flooding" and `spark` miss "sparking", so every keyword would need every form seeded
        and the ones nobody thought of would silently under-triage.
      - Bangla attaches case and postposition endings directly to the stem (তার → তারে/তারের), so
        the same trailing anchor rejects the form that actually appears in written text.

    The cost is honest and belongs in the admin's hands (FR-30): a *short generic stem* over-matches
    — seed `car` and it fires on "carpet". That is why the seeded list in
    `migrations/0003_severity_keyword.py` uses specific phrases (`live wire`, `open manhole`) rather
    than bare stems, and why `SeverityKeyword.term` carries a minimum length.

    ⚠️ **The one inflection a prefix match cannot absorb is the dropped silent `e`**: English writes
    "collapsing", not "collapseing", so `collapse` shares only `collaps` with it and the match fails.
    A term ending in `e` therefore needs its `-ing` form seeded as its own row. Nothing here can
    detect that — the report classifies, one band too low, with a rationale that reads plausibly —
    so it is a rule for whoever edits the keyword list, not a branch in this function.

    `(?<!\\w)` rather than `\\b` because `\\b` is context-dependent — its meaning flips with the
    first character of the term — while the lookbehind asserts one thing regardless of what follows
    it: nothing word-like immediately precedes the match.
    """
    return re.compile(rf"(?<!\w){re.escape(term)}")


@dataclass(frozen=True, slots=True)
class KeywordRule:
    """One `SeverityKeyword` row, as the engine sees it (data-model §14).

    A plain value object rather than the model: it is what keeps this module Django-free, and it is
    what lets a test state a rule in one line instead of migrating a database to insert one.
    """

    # Already normalized — `selectors.active_keyword_rules()` reads a column that `save()`
    # normalized, and `KeywordFallbackClassifier` normalizes again on the way in so a
    # hand-constructed test rule behaves identically.
    term: str
    severity: Severity
    # data-model §14 — "many → 0..1 Category". `None` is a real and useful state: `injured` or
    # `explosion` raises severity without saying which department owns the problem, and forcing a
    # category on it would push unrelated reports into whichever node the phrase was filed under.
    category: str | None = None
    # ⚠️ **Recorded and deliberately never consulted for matching.** A1/FR-12 make code-mixed
    # "Banglish" a first-class input, so an English indicator routinely appears in a report the
    # citizen marked `bn` — filtering rules by `request.language` would miss exactly the case
    # bilingual understanding exists for. The field is here for the admin and the logs.
    language: str = ""


class KeywordFallbackClassifier(ClassificationService):
    """Assign a category and severity from admin-managed bilingual keywords (FR-13a).

    Deterministic in the strong sense FR-13a needs: the same text and the same rule set produce the
    same answer, whatever order the rules arrive in. Every tie is broken by a total order (see
    `_ranked`), so nothing depends on the queryset's ordering or on dict insertion.
    """

    def __init__(
        self,
        rules: Sequence[KeywordRule],
        *,
        matched_confidence: float = DEFAULT_MATCHED_CONFIDENCE,
        unmatched_confidence: float = DEFAULT_UNMATCHED_CONFIDENCE,
        default_severity: Severity = DEFAULT_SEVERITY,
    ) -> None:
        """Compile the rule set once.

        Args:
            rules: The active keywords. Terms are re-normalized here, so a caller may pass
                hand-written rules without knowing the storage convention.
            matched_confidence: Reported when at least one rule fires.
            unmatched_confidence: Reported when none do.
            default_severity: The band for a report no rule matched.
        """
        # ⚠️ Blank terms dropped rather than compiled: `_compile("")` yields a pattern that matches
        # at every position, so one empty row would mark every report as matched at whatever
        # severity it carried — a single bad admin edit silently re-triaging the whole city.
        #
        # ⚠️ **The stored rule carries the *normalized* term, not the one it was handed.** Everything
        # downstream reads `rule.term`: `_ranked` measures specificity by its length and `_rationale`
        # quotes it to an Authority. Keeping the raw spelling would make two rules that match
        # identically (`flood` and `Flood`) rank and read as two distinct pieces of evidence, and
        # would quote a phrase in a form that never appeared in the report.
        self._rules: tuple[tuple[KeywordRule, re.Pattern[str]], ...] = tuple(
            (replace(rule, term=term), _compile(term))
            for rule, term in ((rule, normalize_term(rule.term)) for rule in rules)
            if term
        )
        self._matched_confidence = matched_confidence
        self._unmatched_confidence = unmatched_confidence
        self._default_severity = default_severity

    def classify(self, request: ClassificationRequest) -> Classification:
        """Match the report text against every rule and derive one answer.

        ⚠️ **Every rule, regardless of its language** — see `KeywordRule.language`.
        """
        haystack = normalize_term(request.text)
        matched = [rule for rule, pattern in self._rules if haystack and pattern.search(haystack)]

        if not matched:
            return Classification(
                category=UNCATEGORIZED_SLUG,
                severity=self._default_severity,
                confidence=self._unmatched_confidence,
                source=ClassifierSource.FALLBACK,
                model=FALLBACK_MODEL,
                rationale=(
                    f"no severity keyword matched; defaulted to {self._default_severity} "
                    f"in the '{UNCATEGORIZED_SLUG}' category"
                ),
            )

        ranked = self._ranked(matched)
        return Classification(
            category=self._category_for(ranked, request.allowed_categories),
            # BR-11's "highest" applied within one report's matches: a body naming both a pothole
            # and a live wire is about the live wire.
            severity=highest(rule.severity for rule in ranked),
            confidence=self._matched_confidence,
            source=ClassifierSource.FALLBACK,
            model=FALLBACK_MODEL,
            rationale=_rationale(ranked),
        )

    @staticmethod
    def _ranked(matched: Sequence[KeywordRule]) -> list[KeywordRule]:
        """Order matches most-severe, then most-specific, then alphabetically.

        ⚠️ **A total order, and that is what makes FR-13a's "deterministic" true.** Sorting only by
        severity leaves ties resolved by whatever order the queryset returned, so the same report
        could land in `roads` today and `public_structures` after an unrelated index change. Length
        descending is the specificity tie-break — `gas leak` should decide the category over `leak`
        — and the term itself is the final, arbitrary-but-fixed discriminator.
        """
        return sorted(
            matched,
            key=lambda rule: (
                # `list(Severity)` is precedence order by declaration — see `contracts.Severity`.
                list(Severity).index(rule.severity),
                -len(rule.term),
                rule.term,
                rule.category or "",
            ),
        )

    @staticmethod
    def _category_for(ranked: Sequence[KeywordRule], allowed: Sequence[str]) -> str:
        """Pick the category of the highest-ranked match that names an allowed one.

        ⚠️ **Skips a rule whose category is not allowed and keeps looking, rather than coercing
        the whole report to the sink.** A keyword can point at a node that has since been retired
        (data-model §5 — categories retire, never delete), and `coerce_category()` on that first
        candidate would drop a report into `other` while a perfectly good second match named a
        live node. `selectors.active_keyword_rules()` already filters retired categories out; this
        is the second belt, because the engine is public and takes whatever it is handed.
        """
        for rule in ranked:
            if rule.category and rule.category in allowed:
                return rule.category
        # Nothing usable — the documented sink (PRD §331). Routed through `coerce_category()` so
        # the "off-taxonomy lands in `other`" rule has exactly one implementation.
        return coerce_category(None, allowed)


def _rationale(ranked: Sequence[KeywordRule]) -> str:
    """Render the matched indicators in the shape FR-14/FR-15 ask to see.

    FR-14's acceptance criterion is "flagged High: 'live wire', 'children'" and API §6.5 shows
    `"phrases: 'danger','hospital'"` — a human-readable string naming the phrases, not a score.
    The `→ band` suffix is what makes it *explainable* rather than merely quoted: an Authority can
    see which phrase drove the band.

    ⚠️ Duplicate terms collapse. Two rules can normalize to the same term (different languages,
    say), and listing a phrase twice reads as corroboration the engine never found.
    """
    seen = dict.fromkeys(f"'{rule.term}' → {rule.severity}" for rule in ranked)
    shown = list(seen)[:MAX_RATIONALE_TERMS]
    suffix = f" (+{len(seen) - len(shown)} more)" if len(seen) > len(shown) else ""
    return "matched " + ", ".join(shown) + suffix
