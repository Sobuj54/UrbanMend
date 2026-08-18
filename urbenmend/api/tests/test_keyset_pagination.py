"""`KeysetCursorPagination` mechanics — the composite-cursor primitive (T7.1, API §4.4).

`api/tests/test_pagination.py` covers the `{data, page, meta}` envelope against a stand-in row list.
This module covers the part of the keyset paginator that a shape assertion cannot see: the
**lexicographic predicate**, the **direction algebra**, and the **cursor codec**.

⚠️ **The private helpers are addressed directly, and deliberately.** Every bug this class exists to
prevent — a dropped equality pin, a flipped comparison under `reverse`, an `int` that comes back as a
`str` on page 2 — is observable end-to-end only as "some row was skipped", which is what
`issues/tests/test_list.py` asserts over a real PostGIS table. Asserting the predicate's *shape*
here means a failure names its cause instead of leaving a reader to infer it from a missing UUID.

⚠️ **No database.** These are pure functions over `Q` objects and query strings; the DB-backed
correctness proof belongs with the collection that uses it.

[doc: API §4.4, §1.3; NFR-2]
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from django.db.models import Q
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from urbenmend.api.pagination import (
    KeysetCursorPagination,
    StandardCursorPagination,
    _decode_value,
    _encode_value,
)

factory = APIRequestFactory()

# The Issue queue's default key set (`issues/pagination.py`), used here because it is the one that
# exercises every branch: a descending leading key, an ascending secondary, and a unique tail.
QUEUE_KEYS: tuple[tuple[str, bool], ...] = (
    ("severity_rank", True),
    ("opened_at", False),
    ("pk", False),
)

OPENED_AT = datetime(2026, 8, 18, 9, 30, tzinfo=UTC)
ROW_ID = UUID("11111111-2222-3333-4444-555555555555")
POSITION: list[Any] = [3, OPENED_AT, ROW_ID]


def _paginator(keys: tuple[tuple[str, bool], ...] = QUEUE_KEYS) -> KeysetCursorPagination:
    paginator = KeysetCursorPagination()
    paginator.keys = keys
    return paginator


def _request(query: str = "") -> Request:
    return Request(factory.get(f"/api/v1/issues{query}"))


def _branches(predicate: Q) -> list[list[str]]:
    """The lookup keys of each `OR` branch, most significant branch first.

    `Q.__or__` flattens a single-child operand into the parent's children as a bare `(lookup, value)`
    tuple, so the first branch arrives in a different shape from the rest. Normalizing that here is
    what keeps the assertions below about the *predicate* rather than about Django's node algebra.
    """
    assert predicate.connector == Q.OR
    branches: list[list[str]] = []
    for child in predicate.children:
        child_any: Any = child
        if isinstance(child_any, tuple):
            branches.append([child_any[0]])
        else:
            branches.append([leaf[0] for leaf in child_any.children])
    return branches


# --------------------------------------------------------------------------------------
# The cursor value codec
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 3, -1, OPENED_AT, ROW_ID, "high", ""])
def test_a_sort_key_survives_the_round_trip_as_its_own_type(value: Any) -> None:
    """⚠️ **Type-preserving, which `str()` is not.**

    A cursor crosses the wire as text. `int`, `datetime` and `UUID` all `str()` into something that
    looks fine, and whether the ORM coerces it back depends on which field the key happens to
    annotate — an invisible dependency on a *future* sort key's column type. The tag makes the
    decode explicit, so a mismatch fails loudly instead of comparing `4` against `"4"`.
    """
    assert _decode_value(_encode_value(value)) == value
    assert type(_decode_value(_encode_value(value))) is type(value)


def test_a_boolean_is_refused_as_a_sort_key() -> None:
    """⚠️ **`bool` is checked before `int`, because in Python it *is* one.**

    Left to the `int` branch, `True` encodes as `i:True` and then raises inside `int()` on the way
    back — on page two, never page one, and only for whichever deployment had a boolean key. It is
    also never a legitimate key: two values cannot break a tie.
    """
    with pytest.raises(TypeError):
        _encode_value(True)


def test_an_untagged_type_is_refused_rather_than_stringified() -> None:
    """A `float` key (a distance-ordered collection, say) has to add its own tag. Falling back to
    `str()` would silently lose precision and re-introduce the coercion guesswork above."""
    with pytest.raises(TypeError):
        _encode_value(1.5)


@pytest.mark.parametrize("token", ["x:1", "i:not-a-number", "t:yesterday", "u:not-a-uuid", ""])
def test_a_malformed_token_raises_instead_of_decoding_to_something(token: str) -> None:
    with pytest.raises(ValueError):
        _decode_value(token)


# --------------------------------------------------------------------------------------
# Direction algebra
# --------------------------------------------------------------------------------------


def test_order_by_preserves_each_keys_own_direction() -> None:
    """⚠️ **Mixed directions are the second reason this class exists.** `severity DESC, opened_at
    ASC, id ASC` is the ordering §6.5 mandates and the one DRF's single-comparison cursor cannot
    represent."""
    assert _paginator()._order_by(reverse=False) == ("-severity_rank", "opened_at", "pk")


def test_order_by_inverts_every_key_when_walking_backwards() -> None:
    """Each key flips, not just the leading one — a partial inversion would order the backward page
    by a different rule than the forward one and quietly drop the rows in between."""
    assert _paginator()._order_by(reverse=True) == ("severity_rank", "-opened_at", "-pk")


def test_the_comparison_operator_follows_the_same_xor_as_the_ordering() -> None:
    """`descending != reverse` decides both the `order_by` sign and the `__gt`/`__lt` lookup. If the
    two ever disagreed the page would be *ordered* one way and *filtered* the other, which returns
    rows already served — the failure cursors are chosen over offsets to avoid (§4.4)."""
    forward = _branches(_paginator()._position_filter(POSITION, reverse=False))
    backward = _branches(_paginator()._position_filter(POSITION, reverse=True))

    assert [branch[0] for branch in forward] == [
        "severity_rank__lt",
        "opened_at__gt",
        "pk__gt",
    ]
    assert [branch[0] for branch in backward] == [
        "severity_rank__gt",
        "opened_at__lt",
        "pk__lt",
    ]


# --------------------------------------------------------------------------------------
# The lexicographic predicate
# --------------------------------------------------------------------------------------


def test_every_branch_pins_all_preceding_keys_to_equality() -> None:
    """⚠️ **The one assertion that catches the tempting simplification.**

    OR-ing bare per-key comparisons (`rank < r0 OR opened_at > t0`) reads as equivalent and is not:
    it matches rows in a *more severe* band whose timestamp happens to be later, so page two
    re-serves rows from page one. Each branch must compare its own key and hold every more
    significant key equal.
    """
    branches = _branches(_paginator()._position_filter(POSITION, reverse=False))

    assert branches == [
        ["severity_rank__lt"],
        ["opened_at__gt", "severity_rank"],
        ["pk__gt", "severity_rank", "opened_at"],
    ]


def test_a_single_key_predicate_is_one_plain_comparison() -> None:
    """A one-key paginator (the class default) must not grow an empty equality pin — `Q()` ANDed in
    would be a no-op today and a `WHERE TRUE` waiting to be "tidied" into something wrong."""
    predicate = _paginator((("pk", True),))._position_filter([ROW_ID], reverse=False)

    assert predicate == Q(pk__lt=ROW_ID)


def test_the_predicate_names_the_declared_keys_and_nothing_else() -> None:
    """A key set is data, and the predicate is generated from it — so adding `?sort=` cases cannot
    require touching the comparison logic. Asserted against a second key set for that reason."""
    branches = _branches(
        _paginator((("corroboration_total", True), ("pk", False)))._position_filter(
            [7, ROW_ID], reverse=False
        )
    )

    assert branches == [["corroboration_total__lt"], ["pk__gt", "corroboration_total"]]


# --------------------------------------------------------------------------------------
# Cursor encode/decode as a pair
# --------------------------------------------------------------------------------------


def test_no_cursor_means_the_first_page_rather_than_an_error() -> None:
    assert _paginator()._decode_cursor(_request()) is None
    assert _paginator()._decode_cursor(_request("?cursor=")) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_an_encoded_position_decodes_to_itself_including_its_direction(reverse: bool) -> None:
    """The direction travels *inside* the token, not as a separate query param a client could flip
    independently of the position it was minted for."""
    token = _paginator()._encode_cursor(POSITION, reverse=reverse)

    decoded = _paginator()._decode_cursor(_request(f"?cursor={token}"))

    assert decoded == (POSITION, reverse)


def test_the_token_is_opaque_rather_than_a_readable_offset() -> None:
    """§1.2: an opaque token, not a number a client could increment. Base64 is not a security
    boundary and is not claimed as one — it is what stops a client from *depending* on the shape."""
    token = _paginator()._encode_cursor(POSITION, reverse=False)

    assert "severity_rank" not in token
    assert str(ROW_ID) not in token
    assert "://" not in token


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!!",  # not base64 at all
        "eA==",  # valid base64, not a query string
        "cD1pOjM=",  # a position with no direction — `p` present, `r` missing
    ],
)
def test_an_unreadable_cursor_is_refused_and_never_silently_restarts(cursor: str) -> None:
    """⚠️ **A truncated token must not fall back to page one.** A client deep in a long queue would
    receive the first page again with no way to tell it from having wrapped, turning a broken cursor
    into an infinite scroll.

    `NotFound` rather than a `400`: it is DRF's own answer for `invalid_cursor_message` and what
    `/reports` already returns for a bad cursor. One behaviour across every collection beats a
    tidier status code on this one.
    """
    with pytest.raises(NotFound):
        _paginator()._decode_cursor(_request(f"?cursor={cursor}"))


def test_a_cursor_minted_under_a_different_sort_is_refused() -> None:
    """⚠️ **Arity is checked, because a `?sort=` switch mid-scroll is a real client sequence.** A
    two-value position fed to a three-key paginator would otherwise build a predicate over the wrong
    columns and answer `200` with rows from nowhere in particular."""
    token = _paginator((("opened_at", False), ("pk", False)))._encode_cursor(
        [OPENED_AT, ROW_ID], reverse=False
    )

    with pytest.raises(NotFound):
        _paginator()._decode_cursor(_request(f"?cursor={token}"))


# --------------------------------------------------------------------------------------
# Class-level guarantees
# --------------------------------------------------------------------------------------


def test_paging_a_materialized_sequence_is_refused_loudly() -> None:
    """The position is a SQL predicate. Handed a list, the only available fallback is slicing — i.e.
    offset paging, silently, on the one endpoint §4.4 forbids it for."""
    with pytest.raises(TypeError, match="QuerySet"):
        _paginator().paginate_queryset([{"pk": 1}], _request())


def test_the_inherited_created_at_ordering_is_blanked() -> None:
    """⚠️ `StandardCursorPagination.ordering` names `created_at`, which `Issue` does not have.
    `paginate_queryset` is a full replacement so the value is never read — but leaving it would make
    any future `super()` call fail with a `FieldError` about a column nobody chose."""
    assert KeysetCursorPagination.ordering == ()


def test_the_default_key_set_ends_in_a_unique_column() -> None:
    """A non-unique final key cannot separate two rows: paging either stalls on the tie or serves it
    twice. Every `?sort=` key set must end in `pk`, and the class default is the example."""
    assert KeysetCursorPagination.keys[-1][0] == "pk"


def test_the_envelope_and_schema_are_inherited_untouched() -> None:
    """The keyset class replaces the *mechanics* only. `{data, page, meta}`, the bare-token cursor
    and the OpenAPI override are `StandardCursorPagination`'s, so a subclass cannot drift the
    contract shape while changing how it positions."""
    assert issubclass(KeysetCursorPagination, StandardCursorPagination)
    schema = KeysetCursorPagination().get_paginated_response_schema({"type": "array"})
    assert schema["required"] == ["data", "page", "meta"]
