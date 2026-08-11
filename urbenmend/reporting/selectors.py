"""
Reporting — read operations.

Query functions for this module. Kept separate from services.py so reads never acquire
write-path side effects, and so the modules that consume this one have a single documented
surface to call [doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

[doc: Arch §3 (FR-5, FR-8, FR-9, FR-11); API §6.3]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.measure import Distance
from django.db.models import Q
from django.http import Http404

from urbenmend.api.exceptions import Gone
from urbenmend.identity.models import Role
from urbenmend.identity.services import has_role, require_role, scoped_category_ids
from urbenmend.media.selectors import visible_media_prefetch
from urbenmend.reporting.models import Report, ReportStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from django.contrib.gis.geos import Point
    from django.db.models import QuerySet

    from urbenmend.identity.models import User

# ⚠️ **The two moderation outcomes, named once.** FR-31 gives an Admin `hide` and `remove`; both
# take a report out of public circulation, and both must do so through the *same* constant, because
# a list endpoint that excluded only one of them would keep republishing the content the other one
# suppressed. `Report.is_editable` deliberately does not reuse this — that is a different question
# (may the author still change it?) with a different answer set.
MODERATED_STATUSES = frozenset({ReportStatus.HIDDEN, ReportStatus.REMOVED})


def get_report_for_read(*, report_id: UUID | str) -> Report:
    """`GET /reports/{id}` — the public read (API §6.3, Q7 RESOLVED: reports are public).

    ⚠️ **No actor parameter, and that is the contract rather than an omission.** §6.3 marks this
    endpoint `Auth: none (public)`. Adding an actor here would imply a per-caller rule that does not
    exist and would drift the moment someone assumed it was being applied. §6.3's error list does
    name `FORBIDDEN`, but "or public" in the same line makes it unreachable — an unreachable status
    is not a case to invent a rule for. `get_media_for_read()` records the same decision.

    ⚠️ **`404` for absent, `410` for moderated — a disclosure decision, not a nicety.** `404` for a
    moderated report leaves a client retrying forever and erases the fact that moderation acted;
    `410` for an id that never existed confirms to a scanner that the id had once been valid. Only a
    surviving row can answer `410`, which is why FR-31 is a state change (database.md "no hard
    deletes").

    ⚠️ **`HIDDEN` answers `410`, not `404`, and the spec is explicit about it.** api-conventions.md's
    table reads `404` as "absent **or hidden from this caller**" — but that row is about *scope*
    hiding (an Authority reading outside its categories), while §6.13 says of the moderation actions
    that "subsequent public GETs return `410 Gone`" without distinguishing hide from remove. Reading
    the conventions table as covering FR-31's `hide` would put the two moderation outcomes on two
    different status codes for no client-visible reason.

    `Http404` rather than DRF's `NotFound` so this module stays DRF-free; the handler renders both
    into the same §4.1 envelope.
    """
    try:
        report = (
            Report.objects.select_related("category")
            .prefetch_related(visible_media_prefetch())
            .get(pk=report_id)
        )
    except (Report.DoesNotExist, ValueError, TypeError) as exc:
        # `ValueError`/`TypeError` cover a malformed UUID reaching a caller outside the URL
        # converter (a management command, a test). A bad id is "not found", never a `500`.
        raise Http404("Report not found.") from exc

    if report.status in MODERATED_STATUSES:
        raise Gone

    return report


def list_reports(
    *,
    actor: User,
    statuses: Sequence[str] = (),
    category_slugs: Sequence[str] = (),
    query: str = "",
    near: Point | None = None,
    radius_m: float | None = None,
) -> QuerySet[Report]:
    """`GET /reports` — the caller's visible reports, filtered (API §6.3).

    ⚠️ **Visibility is decided here, before any filter is applied** (FR-3, Arch §3.1). §6.3 fixes
    three rules — "Citizen: own reports · Authority: reports in scope · Admin: all" — and they are
    not preferences a query param may widen. A filter appended after a `Report.objects.all()` by a
    later caller would leak every report in the city; starting from the scoped queryset means the
    worst a bad filter can do is return too *few* rows.

    ⚠️ **`require_role()` and not a bare `is_authenticated` check.** `has_role()` reads `status` as
    well as `role` (T1.5), so a suspended Authority — who still has `role == "authority"` — is
    refused here rather than served their old queue.

    ⚠️ **An Authority's scope is over `category`, and an unclassified report has none.** A report
    sits at `category IS NULL` between submission and T3.5's triage, so `category_id__in=<scope>`
    excludes it from every Authority's list. That is correct and it is the point: a report nobody has
    categorized yet belongs to no department. It is *not* an oversight to be patched by widening the
    filter to `Q(category__isnull=True)`, which would put every un-triaged report in every
    Authority's queue at once.

    ⚠️ **`None` from `scoped_category_ids()` means Admin/unrestricted, and it is handled by the
    branch above rather than by this filter.** Collapsing the two — `filter(category_id__in=scope)`
    with `scope=None` — is a `TypeError`, but the tempting "fix", treating a falsy scope as
    unrestricted, silently promotes an Authority with an empty scope to Admin. Empty grants nothing
    (T1.5); `or set()` here is the fail-closed spelling of that.

    ⚠️ **Moderated reports are excluded for every role, Admin included.** Rendering a hidden row
    inside a list would put back exactly the content FR-31 suppressed, and there is no id in a list
    request to answer `410` about. The consequence is real and worth stating: a citizen whose report
    was moderated sees it disappear from their own list with no explanation. Surfacing it to the
    author only would leak the moderation decision into a public-shaped payload; the moderation
    review surface is §6.13's, not this one.

    Returns a queryset, not a list — the paginator has to slice it, and `ReportCursorPagination`
    applies the ordering (its `-pk` tie-break must move with `?sort=`).
    """
    require_role(actor, Role.CITIZEN, Role.AUTHORITY, Role.ADMIN)

    queryset = (
        Report.objects.exclude(status__in=MODERATED_STATUSES)
        .select_related("category")
        # ⚠️ **One query for the whole page's `media[]`, not one per row.** §6.3's list items carry
        # the same `media[]` the detail body does, so without this a 20-item page issues 21 queries
        # and NFR-2's p95 budget is spent on a loop no reader of the serializer would notice.
        .prefetch_related(visible_media_prefetch())
    )

    if has_role(actor, Role.ADMIN):
        pass  # No filter at all — §6.3 "Admin: all".
    elif has_role(actor, Role.AUTHORITY):
        queryset = queryset.filter(category_id__in=scoped_category_ids(actor) or set())
    else:
        queryset = queryset.filter(author=actor)

    if statuses:
        queryset = queryset.filter(status__in=statuses)

    if category_slugs:
        # Slug, not id: `?category=roads` is what §6.2/§6.3 document, and the slug is the machine
        # key (T0.10). Retired categories are deliberately still filterable — reports classified
        # before a retirement keep pointing at it, and they must stay findable.
        queryset = queryset.filter(category__slug__in=category_slugs)

    if query:
        # ⚠️ **`icontains`, and this is the honest limit of `?q=` today.** §1.4 wants bilingual
        # free-text search; PostgreSQL ships no Bangla text-search configuration, so a
        # `SearchVector` over `'simple'` would stem English and do nothing at all for Bangla while
        # *looking* like full-text search. A substring match is worse at English and identical for
        # Bangla, and it does not pretend otherwise. `address` is included because "Mirpur Road" is
        # a search a citizen will type and it is not in the description.
        queryset = queryset.filter(Q(description__icontains=query) | Q(address__icontains=query))

    if near is not None and radius_m is not None:
        # ⚠️ `__dwithin` on the `geography` column takes **metres**, which is the whole reason
        # `location` is `geography=True` (T2.1) — on a `geometry(4326)` column the same lookup would
        # silently interpret `radiusM` as *degrees* and a 500 m search would cover half of Asia.
        # `Distance(m=...)` states the unit at the call site rather than leaving a bare float.
        queryset = queryset.filter(location__dwithin=(near, Distance(m=radius_m)))

    return queryset
