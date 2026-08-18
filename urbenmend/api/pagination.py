"""
Collection envelope and cursor pagination (A8, T0.6).

API §1.3 fixes the shape of every list response:

    {"data": [...], "page": {"nextCursor": ..., "prevCursor": ..., "limit": 20},
     "meta": {"count": 20}}

DRF's `CursorPagination` emits `{"next": <url>, "previous": <url>, "results": [...]}`, so the
class is subclassed for its cursor *mechanics* and its response shape is replaced entirely
[doc: API §1.3/§4.4, Plan T0.6].

**Cursor, not offset, is a correctness requirement rather than a preference** (API §4.4): the
authority queue is sorted and mutated concurrently, so an offset page-2 silently skips rows
that moved and repeats rows that arrived. Cursor paging is stable across those mutations.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

from django.db.models import Q, QuerySet
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rest_framework.request import Request
    from rest_framework.views import APIView


class StandardCursorPagination(CursorPagination):
    """The project-wide default. Every collection paginates (NFR-2, API §4.4)."""

    page_size = 20
    page_size_query_param = "limit"
    max_page_size = 100
    cursor_query_param = "cursor"

    # ⚠️ A tie-broken, stable ordering is mandatory for cursor pagination to be correct.
    # `-created_at` alone is not unique — two rows sharing a timestamp can straddle a page
    # boundary and be skipped or repeated, which is the very bug cursors are chosen to avoid.
    # `-pk` breaks the tie. A view whose model has no `created_at` MUST override this.
    #
    # ⚠️ **Annotated `tuple[str, ...]`, not left to inference.** mypy would infer `tuple[str, str]`
    # from this literal and then reject every subclass whose override has a different length —
    # including `KeysetCursorPagination`'s empty tuple below.
    ordering: tuple[str, ...] = ("-created_at", "-pk")

    def get_paginated_response(self, data: Any) -> Response:
        return Response(
            OrderedDict(
                (
                    ("data", data),
                    (
                        "page",
                        OrderedDict(
                            (
                                # Opaque by construction — DRF's cursor is an encoded position,
                                # not an offset a client could increment (API §1.2/§4.4).
                                ("nextCursor", self._cursor_token(self.get_next_link())),
                                ("prevCursor", self._cursor_token(self.get_previous_link())),
                                ("limit", self._effective_limit()),
                            )
                        ),
                    ),
                    # `count` is the size of THIS page, matching the §1.3 example where a
                    # 20-item page reports 20. Not a total: a total needs a second COUNT(*)
                    # over the whole filtered set on every request, which NFR-2's p99 budget
                    # will not absorb on the issue list — and cursor paging never exposes a
                    # page count for it to feed.
                    ("meta", OrderedDict((("count", len(data)),))),
                )
            )
        )

    def _effective_limit(self) -> int:
        """The page size actually applied, for `page.limit`.

        ⚠️ Reports the *clamped* value, not the client's raw `?limit=`. Asking for 500 yields a
        100-item page (`max_page_size`), and echoing 500 back would tell the client the server
        honoured a limit it did not — leaving it to conclude the collection had ended.

        `self.request` is set by `paginate_queryset`, which always runs before this; it is read
        via `getattr` because DRF assigns it dynamically and the stubs do not declare it.
        """
        request = getattr(self, "request", None)
        if request is None:
            return self.page_size
        return self.get_page_size(request) or self.page_size

    def _cursor_token(self, link: str | None) -> str | None:
        """Reduce DRF's absolute next/prev URL to the bare cursor value.

        The contract says `page.nextCursor` is an opaque token the client passes back as
        `?cursor=`, not a URL. Returning DRF's full link would leak the internal host and
        make the client parse a query string to page.
        """
        if link is None:
            return None
        values = parse_qs(urlparse(link).query).get(self.cursor_query_param)
        return values[0] if values else None

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Keep generated OpenAPI in step with the envelope above.

        Without this the schema advertises DRF's `{next, previous, results}` while the server
        sends `{data, page, meta}` — and a generated client would be wrong in a way no test
        here would catch.
        """
        return {
            "type": "object",
            "required": ["data", "page", "meta"],
            "properties": {
                "data": schema,
                "page": {
                    "type": "object",
                    "required": ["nextCursor", "prevCursor", "limit"],
                    "properties": {
                        "nextCursor": {"type": "string", "nullable": True, "example": "cD0yMDI2"},
                        "prevCursor": {"type": "string", "nullable": True, "example": None},
                        "limit": {"type": "integer", "example": self.page_size},
                    },
                },
                "meta": {
                    "type": "object",
                    "required": ["count"],
                    "properties": {"count": {"type": "integer", "example": self.page_size}},
                },
            },
        }


# ⚠️ **Tagged values, not bare `str()`.** A cursor round-trips through a URL as text, and the three
# key types this project sorts on (`int` rank, `datetime`, `UUID`) all `str()` into something that
# looks fine and only some of which the ORM will coerce back. Relying on `IntegerField
# .get_prep_value("4")` to re-parse is an invisible dependency on which field a *future* sort key
# happens to annotate; a one-character tag makes the decode explicit and its failure loud.
def _encode_value(value: Any) -> str:
    # `bool` before `int`: `True` is an `int` in Python, and it would encode as `i:True` and then
    # raise inside `int()` on the way back — at page 2, never page 1.
    if isinstance(value, bool):
        raise TypeError("A boolean is not a usable sort key: it cannot break a tie.")
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, datetime):
        return f"t:{value.isoformat()}"
    if isinstance(value, UUID):
        return f"u:{value}"
    if isinstance(value, str):
        return f"s:{value}"
    raise TypeError(f"Unsupported cursor key type: {type(value).__name__}.")


def _decode_value(token: str) -> Any:
    tag, _, raw = token.partition(":")
    if tag == "i":
        return int(raw)
    if tag == "t":
        parsed = parse_datetime(raw)
        if parsed is None:
            raise ValueError(f"Not an ISO-8601 timestamp: {raw!r}.")
        return parsed
    if tag == "u":
        return UUID(raw)
    if tag == "s":
        return raw
    raise ValueError(f"Unknown cursor value tag: {tag!r}.")


class KeysetCursorPagination(StandardCursorPagination):
    """Cursor paging over a *composite* key, for orderings DRF's cursor cannot express.

    ⚠️ **This class exists because `CursorPagination` compares on `ordering[0]` alone.** Its
    `paginate_queryset` builds one `__gt`/`__lt` from the leading key, and `get_next_link` then
    looks for a row on the page whose leading value differs from the rest. When none does, it falls
    back to an accumulating `offset` — or to `position = self.previous_position` with `offset = 0`,
    which excludes the whole tied group. DRF's own comment there reads *"the change in direction
    will introduce a paging artifact, where we end up skipping forward a few extra items."*

    That fallback is unreachable for `/reports`, whose leading key is a near-unique `created_at`.
    It is **guaranteed** for the Issue queue, whose mandated default sort leads on a four-value
    severity band (API §6.5, FR-22): any full page inside one band has no unique leading position.
    Offset paging on a concurrently-mutated sorted queue is the precise failure §4.4 makes cursors
    mandatory to avoid, so the fix has to be a real keyset cursor rather than a different tie-break.

    A subclass declares `keys` as `(field, descending)` pairs, most significant first, ending in a
    unique column (`pk`). The generated predicate is lexicographic:

        rank < :r0
        OR (rank = :r0 AND opened_at > :t0)
        OR (rank = :r0 AND opened_at = :t0 AND id > :i0)

    ⚠️ **Mixed directions are safe here, and that is the second reason this class exists.** Each
    key's own direction is encoded in its own branch, so `severity DESC, opened_at ASC, id ASC` is
    expressible — the exact combination `ReportCursorPagination` documents as unrepresentable under
    DRF's single-comparison cursor.

    ⚠️ **Honest limit: keyset paging is stable against inserts and deletes, not against a row's own
    sort key changing mid-scroll.** An Authority overriding a severity, or a confirmation arriving,
    moves that Issue relative to the cursor, so it can be seen twice or not at all across a scroll.
    No cursor scheme fixes that — the position *is* the sort key — and it is strictly better than an
    offset, which reshuffles on every insert anywhere in the set.

    ⚠️ **The cursor is opaque but unsigned, and that is not a gap.** It encodes a position in an
    ordering, never a permission: visibility is applied by the selector, upstream and independently,
    so a forged cursor can only reposition a caller inside the rows they could already page through.
    Signing it would imply the token carries authority it must never carry.
    """

    # ⚠️ Never read — `paginate_queryset` below is a full replacement, not an extension. Blanked
    # because the inherited `("-created_at", "-pk")` names a column `Issue` does not have, so a
    # future `super()` call would fail with a confusing `FieldError` rather than a missing ordering.
    ordering = ()

    # `(field, descending)`, most significant first. The last entry MUST be unique per row or the
    # lexicographic predicate cannot separate two rows and paging stalls or repeats.
    keys: tuple[tuple[str, bool], ...] = (("pk", True),)

    # DRF assigns `self.request` dynamically and the stubs do not declare it (see
    # `_effective_limit`), so it is declared here for the type checker rather than read via
    # `getattr` in five places.
    request: Request

    def paginate_queryset(
        self,
        queryset: QuerySet[Any] | Sequence[Any],
        request: Request,
        view: APIView | None = None,
    ) -> list[Any] | None:
        """Fetch one page, positioned by the decoded cursor rather than by an offset."""
        if not isinstance(queryset, QuerySet):
            raise TypeError(
                "KeysetCursorPagination needs a QuerySet: the position is a SQL predicate, "
                "and paging a materialized sequence would have to fall back to slicing."
            )
        self.request = request
        limit = self.get_page_size(request) or self.page_size or 20

        decoded = self._decode_cursor(request)
        position, reverse = decoded if decoded is not None else (None, False)

        if position is not None:
            queryset = queryset.filter(self._position_filter(position, reverse=reverse))
        queryset = queryset.order_by(*self._order_by(reverse=reverse))

        # `limit + 1` is how "is there another page?" is answered without a second COUNT(*) — the
        # extra row is fetched, inspected and discarded.
        rows = list(queryset[: limit + 1])
        beyond = len(rows) > limit
        rows = rows[:limit]

        if reverse:
            # Walking backwards returns rows in inverted order; the client must still receive them
            # in the ordering it asked for.
            rows.reverse()
            # We only got here from a `nextCursor`, so a next page provably exists.
            self.has_next = True
            self.has_previous = beyond
        else:
            self.has_next = beyond
            self.has_previous = position is not None

        self.page = rows
        return rows

    def get_next_link(self) -> str | None:
        if not self.has_next or not self.page:
            return None
        return self._link(self.page[-1], reverse=False)

    def get_previous_link(self) -> str | None:
        if not self.has_previous or not self.page:
            return None
        return self._link(self.page[0], reverse=True)

    def _order_by(self, *, reverse: bool) -> tuple[str, ...]:
        return tuple(
            f"-{field}" if descending != reverse else field for field, descending in self.keys
        )

    def _position_filter(self, position: list[Any], *, reverse: bool) -> Q:
        """The lexicographic "strictly after this position" predicate.

        ⚠️ **Every branch pins all *preceding* keys to equality.** Dropping that and OR-ing bare
        per-key comparisons (`rank < r0 OR opened_at > t0`) reads as equivalent and is not: it
        matches rows in a *more severe* band whose timestamp happens to be later, so the second
        page re-serves rows from the first.
        """
        combined = Q()
        for index, (field, descending) in enumerate(self.keys):
            # Forward through a descending key means smaller values; reversing flips it. The `!=`
            # is an XOR over the two directions, which is why a mixed-direction ordering works.
            operator = "lt" if descending != reverse else "gt"
            branch = Q(**{f"{field}__{operator}": position[index]})
            for earlier in range(index):
                branch &= Q(**{self.keys[earlier][0]: position[earlier]})
            combined |= branch
        return combined

    def _link(self, row: Any, *, reverse: bool) -> str:
        token = self._encode_cursor(
            [getattr(row, field) for field, _ in self.keys],
            reverse=reverse,
        )
        return replace_query_param(
            self.request.build_absolute_uri(), self.cursor_query_param, token
        )

    def _encode_cursor(self, position: list[Any], *, reverse: bool) -> str:
        querystring = urlencode(
            {"p": [_encode_value(value) for value in position], "r": "1" if reverse else "0"},
            doseq=True,
        )
        return b64encode(querystring.encode("utf-8")).decode("ascii")

    def _decode_cursor(self, request: Request) -> tuple[list[Any], bool] | None:
        """The position and direction a `?cursor=` encodes, or `None` on the first page.

        ⚠️ **An unreadable cursor raises, and must never silently restart at page one.** A caller
        paging a long queue would see the first page again and have no way to distinguish it from
        having wrapped around, so a truncated token would turn into an infinite scroll. `NotFound`
        (rather than a `400`) is DRF's own answer for `invalid_cursor_message`, which is what
        `/reports` already returns — one behaviour across every collection beats a tidier code here.
        """
        encoded = request.query_params.get(self.cursor_query_param)
        if not encoded:
            return None
        try:
            querystring = b64decode(encoded.encode("ascii")).decode("utf-8")
            parsed = parse_qs(querystring, keep_blank_values=True, strict_parsing=True)
            position = [_decode_value(token) for token in parsed["p"]]
            reverse = parsed["r"][0] == "1"
        except (ValueError, KeyError, IndexError, UnicodeDecodeError, TypeError) as exc:
            raise NotFound(self.invalid_cursor_message) from exc
        if len(position) != len(self.keys):
            # A cursor minted under a different `?sort=` has the wrong arity. Reusing it would
            # build a predicate over the wrong columns rather than fail.
            raise NotFound(self.invalid_cursor_message)
        return position, reverse
