"""
Collection-envelope tests (A8, T0.6).

API §1.3 fixes the list shape for every collection, so these assert the emitted JSON rather than
DRF's internals: a refactor that keeps the shape passes, one that renames a key fails.

⚠️ These run against `RowList` below rather than a real queryset, and touch no database — the
A7 migration is outstanding, so a DB-backed test would error for a reason unrelated to what it
checks. The trade-off is stated plainly: this verifies the envelope and the paginator's *use* of
the cursor protocol, not Postgres keyset-scan behaviour. The first real paginated collection
(`GET /reports`, P1) is where that gets covered end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, cast

from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from urbenmend.api.pagination import StandardCursorPagination

factory = APIRequestFactory()


class RowList:
    """The narrow slice of the queryset protocol `CursorPagination` actually calls.

    DRF's paginator needs `order_by()`, a single-kwarg `filter()` (`field__gt` / `field__lt`),
    and slicing. Implementing just those keeps these tests off the database.

    ⚠️ Rows are dicts and the sort key is a **zero-padded string**. DRF derives the cursor
    position with `str(attr)` and compares it as text, so unpadded integers would order
    lexicographically (`"10" < "9"`) and the round-trip assertion below would fail against a
    paginator that is behaving correctly.
    """

    def __init__(self, rows: Iterable[dict[str, str]]) -> None:
        self._rows = list(rows)

    def order_by(self, *fields: str) -> RowList:
        field = fields[0]
        key = field.lstrip("-")
        return RowList(sorted(self._rows, key=lambda row: row[key], reverse=field.startswith("-")))

    def filter(self, **kwargs: str) -> RowList:
        ((lookup, value),) = kwargs.items()
        field, _, operator = lookup.partition("__")
        if operator == "gt":
            return RowList(row for row in self._rows if row[field] > value)
        return RowList(row for row in self._rows if row[field] < value)

    def __getitem__(self, index: slice) -> RowList:
        return RowList(self._rows[index])

    def __iter__(self) -> Iterator[dict[str, str]]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def rows(count: int) -> RowList:
    """`count` rows, sortable and individually identifiable."""
    return RowList({"slug": f"item-{index:04d}"} for index in range(count))


def paginate(source: RowList, query: str = "") -> dict[str, Any]:
    """Run rows through the paginator and return the response body.

    The two `cast`s are the price of the stand-in: `RowList` implements the slice of the
    queryset protocol DRF calls, but it is not a `QuerySet`, and `ordering` is typed as a
    2-tuple by the class default. Confined to this helper so no production signature is loosened.
    """
    paginator = StandardCursorPagination()
    # `RowList` has no `created_at`; a real view keeps the class default `-created_at, -pk`.
    paginator.ordering = cast(Any, ("slug",))
    request = Request(factory.get(f"/api/v1/things{query}"))
    page: list[Any] | None = paginator.paginate_queryset(cast(Any, source), request)
    assert page is not None
    return dict(paginator.get_paginated_response(page).data)


def slugs(body: dict[str, Any]) -> list[str]:
    return [row["slug"] for row in body["data"]]


def test_collection_uses_the_data_page_meta_envelope() -> None:
    """The three top-level keys of API §1.3, and nothing else.

    DRF's built-in emits `{next, previous, results}`; the docs name this as one of the two known
    divergences the implementation must correct rather than accept.
    """
    assert list(paginate(rows(5))) == ["data", "page", "meta"]


def test_page_block_carries_both_cursors_and_the_limit() -> None:
    assert set(paginate(rows(50))["page"]) == {"nextCursor", "prevCursor", "limit"}


def test_default_limit_is_twenty() -> None:
    """API §4.4's documented default."""
    body = paginate(rows(50))
    assert body["page"]["limit"] == 20
    assert len(body["data"]) == 20


def test_limit_query_param_is_honoured() -> None:
    body = paginate(rows(50), "?limit=5")
    assert body["page"]["limit"] == 5
    assert len(body["data"]) == 5


def test_limit_is_capped_at_one_hundred() -> None:
    """API §4.4: max 100. NFR-2 — an uncapped limit lets one request scan the table."""
    assert len(paginate(rows(500), "?limit=500")["data"]) == 100


def test_reported_limit_is_the_clamped_value_not_the_request() -> None:
    """`page.limit` must describe what the server did.

    Echoing the requested 500 while returning 100 rows tells the client the collection ended when
    it has not — the client stops paging and silently loses data.
    """
    assert paginate(rows(500), "?limit=500")["page"]["limit"] == 100


def test_next_cursor_is_an_opaque_token_not_a_url() -> None:
    """`page.nextCursor` is the value a client passes back as `?cursor=`.

    DRF's `get_next_link()` returns an absolute URL, which would leak the internal host and force
    every client to parse a query string in order to page.
    """
    cursor = paginate(rows(50))["page"]["nextCursor"]
    assert cursor is not None
    assert "://" not in cursor
    assert "?" not in cursor
    assert "/" not in cursor


def test_cursors_are_null_when_there_is_no_further_page() -> None:
    """`null`, not `""` — §1.3's own example reads `"opaque-or-null"`."""
    body = paginate(rows(3))
    assert body["page"]["nextCursor"] is None
    assert body["page"]["prevCursor"] is None


def test_the_next_cursor_actually_advances_the_page() -> None:
    """Round-trip: feeding the token back yields the following rows, with no overlap.

    A `nextCursor` that is well-formed but positions wrongly is the failure mode that matters —
    it silently drops or duplicates rows, and every shape assertion above would still pass.
    """
    source = rows(50)
    first = paginate(source)
    second = paginate(source, f"?cursor={first['page']['nextCursor']}")

    assert not set(slugs(first)) & set(slugs(second))
    assert slugs(first) == [f"item-{index:04d}" for index in range(20)]
    assert slugs(second)[0] == "item-0020"


def test_paging_forward_then_back_returns_the_first_page() -> None:
    """`prevCursor` is populated off the first page and positions symmetrically.

    Without this, a client can page forward but not back — and the bug is invisible to any test
    that only ever walks in one direction.
    """
    source = rows(50)
    second = paginate(source, f"?cursor={paginate(source)['page']['nextCursor']}")
    assert second["page"]["prevCursor"] is not None

    back = paginate(source, f"?cursor={second['page']['prevCursor']}")
    assert slugs(back) == [f"item-{index:04d}" for index in range(20)]


def test_meta_count_is_the_size_of_this_page() -> None:
    """§1.3's example reports 20 for a 20-item page.

    Deliberately not a grand total: that needs a second `COUNT(*)` over the whole filtered set on
    every request, which NFR-2's p99 budget will not absorb on the issue list.
    """
    body = paginate(rows(50))
    assert body["meta"]["count"] == 20 == len(body["data"])
    assert paginate(rows(3))["meta"]["count"] == 3


def test_the_default_ordering_is_uniquely_tie_broken() -> None:
    """`-created_at` alone is not unique, and a non-unique cursor ordering is a correctness bug.

    Two rows sharing a timestamp can straddle a page boundary and be skipped or repeated — the
    exact failure cursor pagination is adopted to avoid — so the class default must include `pk`.
    """
    ordering = StandardCursorPagination.ordering
    assert ordering is not None
    assert any(field.lstrip("-") == "pk" for field in ordering)


def test_schema_advertises_the_envelope_it_actually_sends() -> None:
    """Generated OpenAPI must not describe DRF's `{next, previous, results}`.

    Nothing else in this suite would catch that mismatch, and a client generated from the wrong
    schema fails at runtime against a server that is behaving correctly.
    """
    schema = StandardCursorPagination().get_paginated_response_schema({"type": "array"})
    assert schema["required"] == ["data", "page", "meta"]
    assert set(schema["properties"]["page"]["properties"]) == {"nextCursor", "prevCursor", "limit"}
