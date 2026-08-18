"""
Issues — cursor pagination for `GET /issues`, the authority work queue (T7.1/T7.2).

The `?sort=` allowlist and the cursor's key set are the same decision, so they live in one module:
a sort name a client may send is exactly a tuple of keys the paginator can page over, and nothing
else. `IssueListQuerySerializer` imports `SORT_CHOICES` from here rather than restating it.

⚠️ **This queue is why `KeysetCursorPagination` exists.** DRF's cursor compares on the leading key
alone, and the mandated default sort leads on a four-value severity band (API §6.5, FR-22) — see
that class's docstring for what silently breaks. Two of the four sorts below lead on a low-cardinality
or non-unique key, so the composite cursor is load-bearing for `severity` and `corroborationCount`
and merely correct for the other two.

[doc: API §1.3, §4.4, §6.5; FR-19, FR-22, NFR-2]
"""

from __future__ import annotations

from urbenmend.api.pagination import KeysetCursorPagination

# The §6.5 `sort` allowlist, in the spelling clients send (api-conventions.md: a leading `-` is
# descending). Issues default to **severity DESC, then age**.
SORT_SEVERITY = "severity"
SORT_AGE = "age"
SORT_NEWEST = "-createdAt"
SORT_CORROBORATION = "corroborationCount"

SORT_DEFAULT = SORT_SEVERITY
SORT_CHOICES = (SORT_SEVERITY, SORT_AGE, SORT_NEWEST, SORT_CORROBORATION)

# ⚠️ **`(field, descending)` over *annotation* names, not model fields, for the two derived keys.**
# `severity_rank` and `corroboration_total` are annotated by `list_issues()`; a sort naming a key the
# selector does not annotate raises `FieldError` at slice time, which is why the two modules are
# asserted against each other in `test_list.py` rather than trusted to stay in step.
#
# ⚠️ **Every tuple ends in `pk`.** The lexicographic predicate can only separate two rows by a key
# that differs between them; without a unique final key, a page boundary landing inside a tie either
# repeats rows or stalls. `severity_rank` has four values and `corroboration_total` a handful, so for
# those two sorts the tie is the normal case, not an edge case.
#
# ⚠️ **`age` is ascending — oldest first — and that is the whole point of the sort.** FR-19: "allow
# sorting by age, so severe-but-old issues aren't forgotten." Flipping it to newest-first would make
# it a duplicate of `-createdAt` and delete the only view an operator has of a stale backlog. The
# default sort's "then age" reads the same way: within a severity band, the oldest Issue is first.
#
# ⚠️ **`-createdAt` is kept as the published spelling even though `Issue` has no `created_at`** — it
# orders by `opened_at` descending (API §6.5, amended 2026-08-18). Renaming the param to match the
# column would break the documented contract to fix a naming mismatch a client cannot observe.
SORT_KEYS: dict[str, tuple[tuple[str, bool], ...]] = {
    SORT_SEVERITY: (("severity_rank", True), ("opened_at", False), ("pk", False)),
    SORT_AGE: (("opened_at", False), ("pk", False)),
    SORT_NEWEST: (("opened_at", True), ("pk", True)),
    SORT_CORROBORATION: (("corroboration_total", True), ("opened_at", False), ("pk", False)),
}


class IssueCursorPagination(KeysetCursorPagination):
    """`GET /issues` paging, with the cursor's key set chosen by the requested `?sort=`.

    ⚠️ **The sort is passed in by the view, never re-parsed from `request.query_params` here.**
    `IssueListQuerySerializer` has already validated it against `SORT_CHOICES` and answered `400`
    for anything else; reading the raw param a second time would let a value the serializer rejected
    still reach the ordering, and would make two places agree on whitespace and case. Same rule as
    `ReportCursorPagination`.
    """

    def __init__(self, *, sort: str = SORT_DEFAULT) -> None:
        # No `super().__init__()`: DRF's pagination classes define none.
        #
        # ⚠️ `SORT_KEYS[sort]`, deliberately not `.get(sort, SORT_KEYS[SORT_DEFAULT])`. A sort the
        # serializer let through but this table does not know is a bug in the allowlist, and a
        # silent fallback to the default would serve a page ordered by something other than what
        # the client asked for — indistinguishable, from the outside, from working.
        self.keys = SORT_KEYS[sort]
