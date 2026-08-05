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

from collections import OrderedDict
from typing import Any

from rest_framework.pagination import CursorPagination
from rest_framework.response import Response


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
    ordering = ("-created_at", "-pk")

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
        from urllib.parse import parse_qs, urlparse

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
