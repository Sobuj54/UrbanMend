"""
Classification — read operations.

Query functions for this module. Kept separate from services.py so reads never acquire
write-path side effects, and so the modules that consume this one have a single documented
surface to call [doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

⚠️ **This module is the only bridge between the reference tables and the Django-free classifiers.**
`contracts.py`, `keywords.py` and `llm.py` hold no Django imports (T3.1), so every value they need
from the database arrives through a function here, converted to a plain tuple or value object. A
query added inside a classifier is the thing that breaks the provider-swap claim in S1.

[doc: Arch §3, §6 (FR-10, FR-12, FR-13, FR-13a, NFR-13); data-model §5, §14]
"""

from __future__ import annotations

from django.db.models import Q

from urbenmend.classification.contracts import UNCATEGORIZED_SLUG, Severity
from urbenmend.classification.keywords import KeywordRule
from urbenmend.classification.models import (
    Category,
    CategoryStatus,
    SeverityKeyword,
    SeverityKeywordStatus,
)


def active_category_slugs() -> tuple[str, ...]:
    """Every category a classifier may choose from, as `ClassificationRequest` wants them.

    ⚠️ **Read per classification, not cached at import or in a module global.** The taxonomy is
    data (NFR-11), so a node added or retired by a migration must take effect on the next report —
    and the worker is a long-lived process that would otherwise hold a snapshot for as many days as
    it stays up. It is a seven-row table behind a `status` index; the query is not the cost.

    ⚠️ **Retired nodes are excluded** (data-model §5 — retired categories keep historical
    references but accept no new classification). This is the mechanism: the classifier is never
    offered a retired slug, so it cannot pick one, and `coerce_category()` sends anything it does
    pick to the sink.

    Raises:
        RuntimeError: if the `other` sink is missing or retired. ⚠️ **Fails closed, in the shape
            T2.1's `active_city_boundary()` established.** Returning the set anyway would push the
            failure into `ClassificationRequest.__post_init__` — the same outcome, but reported from
            a dataclass three call frames away from the reference-data mistake that caused it. PRD
            §331 makes `other` the landing place for off-taxonomy output, so a deployment without it
            cannot classify at all, and that is worth saying out loud.
    """
    slugs = tuple(
        Category.objects.filter(status=CategoryStatus.ACTIVE)
        .order_by("slug")
        .values_list("slug", flat=True)
    )
    if UNCATEGORIZED_SLUG not in slugs:
        raise RuntimeError(
            f"The {UNCATEGORIZED_SLUG!r} category is missing or retired. It is a required sink for "
            "off-taxonomy classifier output (PRD §331) and for the keyword fallback (FR-13a)."
        )
    return slugs


def active_keyword_rules() -> tuple[KeywordRule, ...]:
    """Load the fallback's rule set (FR-13a, data-model §14).

    Returns plain `KeywordRule` value objects rather than model instances — that conversion is what
    keeps `keywords.py` Django-free (module docstring).

    ⚠️ **Two filters, and the second one is the easy one to forget.** A rule is usable only if the
    rule itself is active *and* the category it points at is still active: a keyword pointing at a
    retired node would otherwise send reports to a category that accepts no new classification
    (data-model §5). Rules with **no** category stay in — a `NULL` category is a real state that
    contributes severity only (see `SeverityKeyword.category`), so `category__status=ACTIVE` alone
    would silently drop `injured` and `gas leak` from the rule set.

    ⚠️ **No `.only()` / no `select_related` shortcut that drops `category__slug`.** The slug is read
    per row here; fetching it through the FK object instead would issue one query per rule on a
    table the fallback reads on every degraded classification.
    """
    rows = (
        SeverityKeyword.objects.filter(status=SeverityKeywordStatus.ACTIVE)
        .filter(Q(category__isnull=True) | Q(category__status=CategoryStatus.ACTIVE))
        .order_by("term")
        .values_list("term", "severity", "category__slug", "language")
    )
    return tuple(
        KeywordRule(
            term=term,
            # The column's `choices` are `SeveritySignal`, whose values are identical to
            # `Severity`'s by asserted test (`test_contracts.py`) — so this is a widening, not a
            # translation. It is `Severity(...)` rather than a bare string because `KeywordRule`
            # declares the enum and mypy is the thing keeping the two aligned at the boundary.
            severity=Severity(severity),
            category=category_slug,
            language=language,
        )
        for term, severity, category_slug, language in rows
    )
