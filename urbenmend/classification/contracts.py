"""
Classification — the provider-facing contract (T3.1).

The single interface every classifier implements: the hosted-LLM adapter (T3.2), the
deterministic keyword fallback (T3.3), and any future vision adapter (FR-13). S1's whole claim is
that the provider is swappable "without touching callers", and that claim is only true if callers
depend on *this* module rather than on any implementation [doc: Arch §6, plan T3.1].

⚠️ **No Django imports in this module, and none in the implementations' engines either.** This is
the load-bearing constraint of T3.1, not a stylistic preference:

  - It is what makes a classifier unit-testable with no database, no settings and no app registry —
    the difference between a test that asserts "'live wire' means critical" in microseconds and one
    that migrates PostGIS first.
  - It is what keeps the provider swap honest. A classifier that reaches for `settings` or runs a
    query has bound itself to this deployment, and S1's "swap the provider" becomes "swap the
    provider and re-wire its data access".
  - It is why the taxonomy arrives as `ClassificationRequest.allowed_categories` instead of being
    queried: the allowed set lives in the `classification_category` table (NFR-11 — taxonomy is
    data, not code), so a Django-free classifier *cannot* read it and the caller must pass it. The
    apparent inconvenience is the constraint doing its job.

⚠️ **`Severity` duplicates `reporting.models.SeveritySignal`, knowingly.** `SeveritySignal` is a
`models.TextChoices` and therefore a Django import; the two cannot be the same object while this
module stays Django-free. The duplication is paid for with
`tests/test_contracts.py::test_severity_matches_the_persisted_enum`, which asserts both the member
values *and* their precedence order against the persisted enum in both directions. Do not "fix"
the duplication by importing from `reporting` — that trades an asserted invariant for a broken one.

[doc: Arch §6; PRD FR-10, FR-12, FR-13a, FR-14, FR-15, NFR-4; plan T3.1]
"""

from __future__ import annotations

import abc
import enum
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

# ⚠️ **The sink node's machine key, and the one taxonomy value this code knows by name.**
# PRD §331 requires a category outside the allowed set to be coerced to `Other / Uncategorized`,
# and FR-13a's fallback needs a terminal bucket when no keyword matches — neither is expressible
# without naming the node. This is why CLAUDE.md marks `other` as a *required* sink that must
# never be retired or deleted: retiring it does not degrade the fallback, it breaks it.
UNCATEGORIZED_SLUG = "other"


class Severity(enum.StrEnum):
    """The four severity bands (FR-14, C-1, Q2 RESOLVED).

    ⚠️ **The declaration order below IS the precedence, most severe first.** Nothing else records
    it: `highest()` derives its ordering from `list(Severity)`, deliberately, so that this module
    holds one statement of the ordering rather than an enum plus a rank table that can disagree.
    Alphabetizing these members — the obvious tidy-up, and what an editor's "sort members" action
    does — silently inverts triage: BR-11's "highest severity among member reports" would start
    returning the *least* severe, and no test outside `test_contracts.py` would notice.

    ⚠️ **`StrEnum`, not `(str, Enum)`.** These values are written into a `CharField` and compared
    against strings read back from Postgres. Under a `(str, Enum)` mixin, `str(Severity.HIGH)` is
    `"Severity.HIGH"`, so any f-string or `str()` on the way to the database stores a value that
    matches no row and no choice — while `==` against `"high"` keeps working, which is exactly the
    combination that survives unit tests and fails in production.

    ⚠️ **Not a score.** FR-21 removed the one numeric priority score; ordering is by label
    (PRD §5.4). The precedence here exists only so "highest" is computable and must never be
    summed, averaged, weighted, or combined with corroboration or proximity — both display-only
    (C-10).
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Derived from declaration order — see the ⚠️ above. Lower index = more severe.
_PRECEDENCE: dict[Severity, int] = {band: index for index, band in enumerate(Severity)}


def highest(severities: Iterable[Severity]) -> Severity:
    """Return the most severe band in `severities`.

    Raises:
        ValueError: if `severities` is empty. ⚠️ **Deliberately not "return LOW on empty".** An
            empty set means the caller matched nothing and has a policy decision to make about
            what an unclassifiable report deserves; answering `LOW` here would make that decision
            invisibly, in a helper, and bury every unrecognised hazard at the bottom of the queue
            (async-worker.md: "Low severity ≠ invisible" cuts the other way too).
    """
    bands = list(severities)
    if not bands:
        raise ValueError("highest() requires at least one severity band.")
    return min(bands, key=lambda band: _PRECEDENCE[band])


def parse_severity(value: object) -> Severity:
    """Coerce an untrusted value to a band, or raise.

    Used on provider output (T3.2): a hosted model asked for `"high"` may answer `"High"`,
    `" high"`, or `"severe"`.

    ⚠️ **Case and whitespace are forgiven; an unknown word is not.** There is no severity sink
    the way `other` is a category sink (PRD §331 names one for categories and nothing names one
    for severity), so a value outside the four bands cannot be coerced — only rejected. Guessing
    a band would have this code invent a life-safety judgement (FR-14 reserves Critical for
    life-safety) on the strength of a provider typo.

    Raises:
        ValueError: if `value` is not one of the four bands.
    """
    if isinstance(value, Severity):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Severity must be a string, got {type(value).__name__}.")
    try:
        return Severity(value.strip().casefold())
    except ValueError as exc:
        raise ValueError(f"Unknown severity band: {value!r}.") from exc


class ClassifierSource(enum.StrEnum):
    """Which automated path produced a classification (FR-10, FR-13a, Arch §6).

    ⚠️ **Two members, where `reporting.ClassificationSource` has four — and the two missing ones
    must stay missing.** `citizen` and `authority` record FR-11's *human* corrections. Giving a
    classifier a way to spell them would let an automated path claim a human made the call, which
    is precisely the distinction NFR-9's fallback-rate KPI, FR-15's explainability, and every
    "did a person review this?" query depend on. The narrow enum is the enforcement.

    Persisted so an operator can tell "the LLM said High" from "the LLM was down and a keyword
    rule said High" (NFR-4 — the product never hard-depends on the external API).
    """

    LLM = "llm"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ClassificationRequest:
    """Everything a classifier is allowed to see about one report.

    ⚠️ **This shape is where P7's PII minimization is enforced — structurally, not by adapter
    discipline.** There is no author, no report id, no coordinate, no address and no contact field
    here, so an adapter *cannot* put them in a prompt however careless it is. A future field is
    therefore a privacy decision, not a convenience: adding `author_id` to "improve caching" would
    ship identifying data to an external provider from every implementation at once.

    The description text itself is exempt from that, unavoidably — it is the thing being
    classified, and a citizen may have typed a name or a phone number into it. That residual is
    why Q9's no-training-data policy is locked, and why the fallback path (which sends nothing
    externally at all) is a privacy win as well as an availability one (RISK-12, Arch §6).
    """

    # FR-5's free text, normalized by the caller only in the sense of being a `str`.
    #
    # ⚠️ **Empty is legitimate and must stay accepted.** BR-3 allows a photo-only submission, so a
    # report with no description is valid input, not a caller bug. A `if not text: raise` here
    # would reject a whole supported submission shape at the classifier boundary — and the honest
    # answer for a photo-only report reaching a text-only classifier is the `other` sink, which is
    # what the fallback already returns.
    text: str

    # The active taxonomy's machine keys (`slug`), as the caller read them at triage time.
    #
    # ⚠️ Passed in rather than queried — see the module docstring. Reading it per request rather
    # than at construction also means a node added by a migration is usable immediately, without
    # restarting a long-lived worker that would otherwise hold a snapshot for days.
    allowed_categories: tuple[str, ...]

    # FR-12 — the submission's language (`en`/`bn`), recorded on the Report at intake.
    #
    # ⚠️ **A hint for prompt wording, never a filter on which keywords apply.** A1/FR-12 make
    # code-mixed "Banglish" a first-class input, so an English indicator routinely appears in a
    # report marked `bn`. `KeywordFallbackClassifier` matches every rule regardless of language
    # for exactly this reason.
    language: str = "en"

    # FR-13 (COULD) — an opaque reference to the report's photo for a vision-capable adapter.
    #
    # ⚠️ **A reference, never image bytes.** Bytes here would put the photo in every log line and
    # every exception payload that repr's a request, and FR-13 is an explicit research stretch: the
    # text adapters ignore this field, and a vision adapter is a *second* implementation of this
    # ABC rather than a branch inside an existing one.
    image_ref: str | None = None

    def __post_init__(self) -> None:
        """Reject a request no classifier could answer honestly.

        ⚠️ **Fails closed on a missing or retired sink, in the shape T2.1's boundary check
        established** (`active_city_boundary()` raises rather than returning `None`, so nobody can
        write `if boundary and ...`). If `other` is absent from the allowed set, coercion (PRD
        §331) has nowhere to land and every unrecognised category would be written to the Report
        as a slug no `Category` row matches. Raising here turns a reference-data mistake into one
        loud failure at triage instead of silent corruption spread across reports.
        """
        if not self.allowed_categories:
            raise ValueError("allowed_categories must name at least one category slug.")
        if UNCATEGORIZED_SLUG not in self.allowed_categories:
            raise ValueError(
                f"allowed_categories must include the {UNCATEGORIZED_SLUG!r} sink; "
                "off-taxonomy output has nowhere to coerce to without it (PRD §331)."
            )


@dataclass(frozen=True, slots=True)
class Classification:
    """One classifier's answer about one report (FR-10).

    ⚠️ **Frozen.** A caller that "adjusts" a confidence or rewrites a rationale after the fact
    breaks FR-15: the stored explanation would no longer be the reasoning that produced the stored
    severity, and an Authority reading "flagged High: 'live wire'" next to a Medium band has no way
    to tell which of the two is wrong.

    ⚠️ **Six fields, where Arch §6's arrow sketches four.** `model` and `rationale` are additive,
    not a departure: FR-10 requires storing "the model/provider + version used", FR-15 requires
    severity to be explainable, and `Report` carries a column for each
    (`classification_model`, `classification_rationale`). Without them on this contract, T3.5 has
    two columns it cannot fill and FR-15 has nothing to show.
    """

    # A slug from the request's `allowed_categories`, already coerced — see `coerce_category()`.
    category: str
    severity: Severity
    # 0.0–1.0. FR-10 stores it; ❓Q10 (the accuracy bar / low-confidence threshold) is **open**, so
    # no threshold is encoded anywhere in this module — T3.7 owns the flagging.
    confidence: float
    source: ClassifierSource
    # FR-10 — "the model/provider + version used". For a hosted adapter, the provider's own model
    # identifier; for the keyword engine, the rule engine's version. ❓Q9 defers the provider, so
    # this is free text rather than an enum of known vendors.
    model: str
    # FR-15 — the key phrases or rule that drove the severity, in the shape API §6.5 shows
    # (`"phrases: 'danger','hospital'"`). Free text, retained verbatim (async-worker.md).
    rationale: str = field(default="")

    def __post_init__(self) -> None:
        """Enforce the contract's own invariants.

        ⚠️ Checked here rather than trusted from each implementation: this is the type that
        crosses into `reporting`, where `severity_signal` is a constrained column and
        `SEVERITY_RANK[...]` is a dict lookup that raises. A confidence of `1.4` or a `model` of
        `""` is not a validation error the API surfaces — it is a row that breaks T4.6's BR-11
        `max()` or an NFR-9 KPI that cannot be grouped by version.
        """
        if not self.category:
            raise ValueError("category must be a non-empty slug.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within 0.0–1.0, got {self.confidence!r}.")
        if not self.model:
            # ⚠️ Required even for the keyword engine, which has no "model". The operator question
            # is "which code path, in which version, decided this" — and an empty string makes
            # NFR-9's LLM-vs-fallback accuracy comparison impossible to break down at all.
            raise ValueError("model must name the deciding provider or rule-engine version.")


def coerce_category(value: object, allowed: Collection[str]) -> str:
    """Return `value` if it is an allowed slug, else the `other` sink (PRD §331).

    Shared by both implementations rather than living in the LLM adapter alone: a hosted model can
    invent a category, and a `SeverityKeyword` row can point at a node that has since been retired
    — the same coercion answers both.

    ⚠️ **Coerces, never raises.** A category outside the taxonomy is the one malformed output PRD
    §331 gives a documented landing place for, and refusing it would fail a classification the
    product can still complete. Severity has no such sink; `parse_severity()` rejects instead.
    """
    if isinstance(value, str):
        candidate = value.strip()
        if candidate in allowed:
            return candidate
    return UNCATEGORIZED_SLUG


class ClassificationError(Exception):
    """Base for every classifier failure.

    ⚠️ **T3.4's degradation catches *this*, not the leaf types.** Catching subclasses individually
    means a failure mode added later escapes the fallback and reaches the worker as an unhandled
    exception — turning "the LLM had a new kind of bad day" into a retry storm and an untriaged
    report. Catching the base makes every future subclass degrade by default, which is the
    direction NFR-4 requires: the product never hard-depends on the external API.
    """


class ClassificationUnavailable(ClassificationError):
    """The classifier could not be reached or was not allowed to run.

    Covers a provider that is unreachable or timed out, an open circuit breaker (T3.6), a
    provider-side rate limit, and the NFR-13 spend/rate cap being exhausted (T3.4) — all of which
    have the same remedy: classify with the keyword fallback instead (FR-13a).
    """


class ClassificationInvalidResponse(ClassificationError):
    """The classifier answered, but the answer cannot be coerced to `Classification`.

    Kept distinct from `ClassificationUnavailable` because the operator response differs: an
    unreachable provider is an incident, whereas a provider returning prose where JSON was asked
    for is a prompt or model-version problem. Both degrade to the fallback (async-worker.md: "on
    timeout **or malformed output**, fall back to keyword rules").
    """


class ClassificationService(abc.ABC):
    """The one interface classifiers implement (S1, Arch §6).

    ⚠️ **A parameter object, where Arch §6 sketches `classify(text, lang?, imageRef?)`.** The
    three sketched values are all present on `ClassificationRequest`; the taxonomy had to join
    them because a Django-free classifier cannot query it (module docstring). Bundling them also
    means T3.4's cache key, T3.6's deadline and T3.7's threshold can arrive without changing this
    signature or every implementation of it.

    ⚠️ **Deliberately not a template method.** An earlier shape had `classify()` concrete, calling
    an abstract `_classify()` and validating the outcome against the request. It was dropped: the
    validation has to either raise (a buggy adapter then fails a classification the fallback could
    have completed, against NFR-4) or silently coerce (a buggy adapter then looks correct forever).
    Implementations call `coerce_category()` and `parse_severity()` themselves, and
    `tests/test_contracts.py` asserts each one honours the allowed set.
    """

    @abc.abstractmethod
    def classify(self, request: ClassificationRequest) -> Classification:
        """Classify one report.

        ⚠️ **Must not raise anything but `ClassificationError`.** T3.4's degradation is written
        against that base (see `ClassificationError`), so a `ValueError`, a `KeyError` or a bare
        `requests` exception escaping an implementation skips the fallback entirely and leaves the
        report untriaged — the exact outcome FR-13a exists to prevent. Wrap, don't leak.

        Raises:
            ClassificationUnavailable: the classifier could not run.
            ClassificationInvalidResponse: the classifier answered unusably.
        """
        raise NotImplementedError
