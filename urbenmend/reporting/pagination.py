"""
Reporting — cursor pagination for `GET /reports` (T2.7).

One class, and it exists for one reason: `?sort=` must move the cursor's tie-break with it.

[doc: API §1.3, §4.4, §6.3; NFR-2]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbenmend.api.pagination import StandardCursorPagination

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from rest_framework.request import Request
    from rest_framework.views import APIView

    from urbenmend.reporting.models import Report

# The §6.3 `sort` allowlist, in the spelling clients send (api-conventions.md: leading `-` is
# descending, and reports default to `-createdAt`).
SORT_DESCENDING = "-createdAt"
SORT_ASCENDING = "createdAt"
SORT_CHOICES = (SORT_DESCENDING, SORT_ASCENDING)


class ReportCursorPagination(StandardCursorPagination):
    """`GET /reports` paging, with the tie-break tied to the requested direction.

    ⚠️ **The `-pk` tie-break has to flip with the sort, and this class exists only because of
    that.** `StandardCursorPagination.ordering` is the fixed `("-created_at", "-pk")`; reusing it for
    `?sort=createdAt` would order rows ascending by timestamp and descending by id inside a
    timestamp. DRF builds its cursor from the *last row's* position under the ordering it was given,
    so a mixed-direction ordering makes the `>`/`<` comparison it generates disagree with the actual
    row order — and the visible symptom is rows silently skipped or repeated at a page boundary,
    which is the exact failure cursor pagination was chosen over offsets to avoid (§4.4). It only
    shows up when two reports share a `created_at`, so it survives every small-fixture test.

    ⚠️ **The direction is passed in by the view, not re-parsed from `request.query_params` here.**
    `ReportListQuerySerializer` has already validated `sort` against the allowlist and answered
    `400` for anything else; reading the raw param a second time would mean a value the serializer
    rejected could still reach the ordering, and two places would have to agree on how to treat
    whitespace and case.
    """

    def __init__(self, *, ascending: bool = False) -> None:
        self.ascending = ascending

    def get_ordering(
        self, request: Request, queryset: QuerySet[Report], view: APIView
    ) -> tuple[str, ...]:
        """Both keys in the same direction — see the class docstring for why."""
        return ("created_at", "pk") if self.ascending else ("-created_at", "-pk")
