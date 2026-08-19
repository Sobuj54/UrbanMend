"""Geospatial Issue read primitives (T4.2) and the authority work queue (T7.1/T7.2).

These functions deliberately express spatial work through GeoDjango so PostGIS can use the
`issues_issue_location_gist` index. Category/time-window matching and open-status rules belong to
T4.3/T4.4; `list_issues()` below carries the public/authority visibility rules for `GET /issues`.

[doc: API §6.5; FR-16, FR-19, FR-22, BR-26]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.gis.db.models.functions import GeometryDistance
from django.contrib.gis.measure import Distance
from django.db.models import (
    Case,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from urbenmend.geo.selectors import nearby_pois
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import has_role, scoped_category_ids
from urbenmend.issues.models import (
    ACTIVE_CORROBORATION_STATUSES,
    ClusteringRule,
    ClusteringRuleStatus,
    Issue,
    IssueStatus,
)
from urbenmend.reporting.models import SEVERITY_RANK, Report
from urbenmend.reporting.selectors import MODERATED_STATUSES as MODERATED_REPORT_STATUSES

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from django.contrib.auth.models import AnonymousUser
    from django.contrib.gis.geos import Point, Polygon
    from django.db.models import QuerySet

# ⚠️ **The two moderation outcomes for an Issue, named once**, for the reason
# `reporting.selectors.MODERATED_STATUSES` records: a list that excluded only one of them would keep
# republishing the content the other suppressed (FR-31). Separate from the Report constant because
# they are separate enums over separate tables, and `?status=` is validated against this one.
MODERATED_ISSUE_STATUSES = frozenset({IssueStatus.HIDDEN, IssueStatus.REMOVED})

# ⚠️ **The attribute `attach_proximity()` populates, named once** — the reason
# `media.selectors.VISIBLE_MEDIA_ATTR` gives: a serializer reading one spelling and a selector writing
# another does not fail, it silently renders an empty list forever. The leading underscore keeps it
# clear of any future `Issue.proximity` field or related name.
PROXIMITY_ATTR = "_proximity_pois"


class ClusteringRuleUnavailable(RuntimeError):
    """No active clustering rule exists for the requested category."""


def active_clustering_rule(*, category_id: int) -> ClusteringRule:
    """Current per-category clustering configuration, read fresh for every decision.

    No module cache is used: an Admin adjustment must affect the next Report without a worker
    restart. The partial unique constraint makes the active row unambiguous.
    """
    try:
        return ClusteringRule.objects.select_related("category").get(
            category_id=category_id,
            status=ClusteringRuleStatus.ACTIVE,
        )
    except ClusteringRule.DoesNotExist as exc:
        raise ClusteringRuleUnavailable(
            f"No active clustering rule is configured for category {category_id}."
        ) from exc


def list_clustering_rules(*, actor: User):
    from urbenmend.identity.services import require_role

    require_role(actor, Role.ADMIN)
    return ClusteringRule.objects.select_related("category").all()


def list_status_events(*, issue_id, actor) -> QuerySet:
    from django.http import Http404

    issue = Issue.objects.select_related("primary_category").filter(pk=issue_id).first()
    if issue is None or issue.status in MODERATED_ISSUE_STATUSES:
        raise Http404("Issue not found.")
    if getattr(actor, "is_authenticated", False) and getattr(actor, "role", None) == Role.AUTHORITY:
        from urbenmend.identity.services import has_category_scope

        if not has_category_scope(actor, issue.primary_category):
            raise Http404("Issue not found.")
    return issue.status_events.select_related("actor").order_by("created_at", "pk")


def issues_within_radius(*, point: Point, radius_m: float) -> QuerySet[Issue]:
    """Issues within `radius_m` metres of `point`, using index-assisted `ST_DWithin`.

    `representative_location` is PostGIS `geography(Point, 4326)`, so the lookup's distance unit is
    metres. The explicit `Distance` value keeps that unit visible at the call site.
    """
    return Issue.objects.filter(representative_location__dwithin=(point, Distance(m=radius_m)))


def matching_open_issues(
    *,
    category_id: int,
    point: Point,
    radius_m: float,
    opened_after: datetime,
    statuses: Collection[str],
) -> QuerySet[Issue]:
    """Nearest matching candidates for T4.4 after the advisory lock is held."""
    return (
        issues_within_radius(point=point, radius_m=radius_m)
        .filter(
            primary_category_id=category_id,
            status__in=statuses,
            opened_at__gte=opened_after,
        )
        .annotate(knn_distance=GeometryDistance("representative_location", point))
        .order_by("knn_distance", "opened_at", "pk")
    )


def nearest_issues(*, point: Point) -> QuerySet[Issue]:
    """Issues ordered nearest-first using PostGIS's GiST-assisted KNN `<->` operator.

    Callers apply their own visibility filters and slice the lazy queryset to the required limit.
    The UUID tie-break makes equidistant rows deterministic without replacing the KNN ordering.
    """
    return Issue.objects.annotate(
        knn_distance=GeometryDistance("representative_location", point)
    ).order_by("knn_distance", "pk")


def _corroboration_subquery() -> QuerySet[Any]:
    """Distinct active people who reported *or* confirmed the outer Issue (FR-16, BR-22).

    ⚠️ **`Count("pk", distinct=True)` is load-bearing.** The `OR` spans two different joins, so a
    user who both reported and confirmed the same Issue appears twice and would be counted twice —
    the one case that makes annotation/property parity a real test rather than a formality.

    ⚠️ **`.order_by()` is required, not tidying.** `User.Meta.ordering` would otherwise be carried
    into the subquery, and PostgreSQL rejects an `ORDER BY` column that is not in the `GROUP BY`.

    The `_group=Value(1)` / `.values("_group")` / `.annotate(...)` dance is the documented Django
    idiom for "aggregate the whole subquery to one row": grouping by a constant collapses the result
    without adding a real column to the `GROUP BY`.

    Returns `QuerySet[Any]` because that idiom ends in `.values()`, which django-stubs types as a
    queryset of `TypedDict` rather than of the model — `QuerySet[User]` would be a lie, and the row
    type is never read here in any case: the value goes straight into `Subquery()`.
    """
    return (
        User.objects.filter(
            Q(reports__issue=OuterRef("pk")) | Q(confirmations__issue=OuterRef("pk")),
            status__in=ACTIVE_CORROBORATION_STATUSES,
        )
        .order_by()
        .annotate(_group=Value(1))
        .values("_group")
        .annotate(total=Count("pk", distinct=True))
        .values("total")
    )


def _report_count_subquery() -> QuerySet[Any]:
    """Member Report count for the outer Issue.

    ⚠️ **Moderated member Reports are deliberately *included*.** `Issue.report_count` is a plain
    `self.reports.count()`, and this annotation exists to be the same number in SQL — "improving" it
    to exclude hidden members would make a 6-report Issue render as 5 for reasons no client could
    see, and would silently change FR-16's corroboration story. Suppressing moderated *content* is
    §6.13's job and happens on the Report resource, not in a count.
    """
    return (
        Report.objects.filter(issue=OuterRef("pk"))
        .order_by()
        .annotate(_group=Value(1))
        .values("_group")
        .annotate(total=Count("pk"))
        .values("total")
    )


def annotate_queue_fields(queryset: QuerySet[Issue]) -> QuerySet[Issue]:
    """Expose the three derived queue values to SQL, so they can be sorted and paged on.

    `Issue.current_severity`, `report_count` and `corroboration_count` are read-only Python
    properties. `?sort=severity` and `?sort=corroborationCount` need them in `ORDER BY` *and* in the
    keyset cursor's `WHERE`, which a property cannot serve.

    ⚠️ **The aliases must not collide with the property names.** Django assigns each annotation onto
    the model instance with `setattr`, and a property with no setter turns that into an
    `AttributeError` on the first row fetched. Hence `current_severity_band`, `report_total` and
    `corroboration_total`.

    ⚠️ **`SEVERITY_RANK` is the source of the `When` clauses, never a second hand-written mapping**
    — two mappings would agree until a band was added to one of them. Ordering is the single use
    that constant's comment sanctions ("nothing more"); the rank is a sort key and is never
    serialized, so no numeric priority score becomes visible (FR-21, C-10).

    ⚠️ **Both counts are correlated subqueries, not `Count()` over a join.** A join aggregate forces
    a `GROUP BY` on the outer query, which interacts badly with the keyset `LIMIT`, and it would put
    the count in `HAVING` — where a cursor predicate cannot reach it. As a `Subquery` the value is an
    ordinary scalar expression usable in `WHERE`, which is what makes `?sort=corroborationCount`
    pageable at all.
    """
    return queryset.annotate(
        # The displayed band: an override wins, and `NULL` means "no override" (never `""`).
        current_severity_band=Coalesce("overridden_severity", "computed_severity"),
    ).annotate(
        severity_rank=Case(
            *(
                When(current_severity_band=band, then=Value(rank))
                for band, rank in SEVERITY_RANK.items()
            ),
            # An unrecognized band sorts last rather than raising. A `KeyError` here would take out
            # the whole queue page for one malformed row, and the row is still worth showing.
            default=Value(0),
            output_field=IntegerField(),
        ),
        corroboration_total=Coalesce(
            Subquery(_corroboration_subquery()), Value(0), output_field=IntegerField()
        ),
        report_total=Coalesce(
            Subquery(_report_count_subquery()), Value(0), output_field=IntegerField()
        ),
    )


def list_issues(
    *,
    actor: User | AnonymousUser,
    category_slugs: Sequence[str] = (),
    severities: Sequence[str] = (),
    statuses: Sequence[str] = (),
    assigned_to_me: bool = False,
    bbox: Polygon | None = None,
    near: Point | None = None,
    radius_m: float | None = None,
    opened_after: datetime | None = None,
    query: str = "",
) -> QuerySet[Issue]:
    """`GET /issues` — the authority work queue, scoped to what this caller may see (API §6.5).

    ⚠️ **No `require_role()`, and that is the contract rather than an omission.** §6.5 marks this
    endpoint "Session or public (Q7 RESOLVED: public)", so the anonymous caller is a first-class
    case; `require_role()` would answer `403` to the public list the spec promises. The visibility
    rules are still applied *here*, before any filter — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing (Arch §3.1, FR-3).

    ⚠️ **A signed-in Authority sees *fewer* Issues than an anonymous visitor, and that is BR-26 as
    written.** §6.5 says "Authority → within category scope", so a `roads` Authority's list omits
    the water-drainage Issues that any logged-out citizen can see. It reads like a bug and is not:
    this endpoint is the *work queue* first, and BR-26 is what makes it one. A future task that wants
    both readings needs a second param and a spec amendment, not a quiet widening here.

    ⚠️ **A suspended Authority falls through to the public branch**, because `has_role()` reads
    `status` as well as `role` (T1.5) — so they get what a logged-out visitor gets rather than their
    old queue. This deliberately diverges from `list_reports()`, which refuses them outright: that
    collection is not public and this one is, so "no queue" is the strongest denial available here
    without contradicting Q7. DRF's `SessionAuthentication` already rejects an inactive user, so in
    practice both layers agree.

    ⚠️ **Moderated Issues are excluded for every role, Admin included, in the selector.** Doing it
    only through the `?status=` allowlist would leave a non-HTTP caller (a management command, a
    later worker) rendering hidden rows, and there is no id in a list request to answer `410` about
    (T2.7 precedent).

    Returns an **unordered** queryset: `IssueCursorPagination` owns the ordering, because the sort
    and the cursor's key set are one decision.
    """
    queryset = annotate_queue_fields(
        Issue.objects.exclude(status__in=MODERATED_ISSUE_STATUSES).select_related(
            # ⚠️ `primary_category` only. `assignee` is deliberately **not** joined: §6.5's
            # `assignedTo` is the assignee's opaque id, which the serializer reads off the
            # `assignee_id` column — a join to fetch a row whose columns are never read is cost with
            # no output. A later task that renders the assignee's *name* has to add the join with it.
            "primary_category"
        )
    )

    # ⚠️ `isinstance(actor, User)`, not `actor.is_authenticated`, only because the other member of
    # the annotated union is `AnonymousUser` — which has no `role` column for `has_role()` to read.
    # The two spellings mean the same thing at runtime; this one narrows the type as well.
    if isinstance(actor, User):
        if has_role(actor, Role.ADMIN):
            pass  # No filter at all — §6.5 "Admin → all".
        elif has_role(actor, Role.AUTHORITY):
            # ⚠️ `or set()` is the fail-closed spelling. `scoped_category_ids()` returns `None` only
            # for an Admin, which the branch above already took; treating a falsy scope as
            # "unrestricted" would promote an empty-scope Authority to Admin (T1.5: empty grants
            # nothing).
            queryset = queryset.filter(primary_category_id__in=scoped_category_ids(actor) or set())
        # Citizen (or a suspended/deprovisioned account) falls through to the public list.

    if category_slugs:
        # Slug, not id — `?category=roads` is the documented spelling and the slug is the machine
        # key (T0.10). Retired categories stay filterable: Issues classified before a retirement keep
        # pointing at it and must remain findable.
        queryset = queryset.filter(primary_category__slug__in=category_slugs)

    if severities:
        # ⚠️ **Matches the *displayed* band, not `computed_severity`.** An Authority who downgraded an
        # Issue to `medium` has to find it under `?severity=medium`; filtering the computed column
        # would return it for the band they explicitly overrode away from — the override would appear
        # to have not taken effect (§6.5's `severity.current` is this value).
        #
        # The `type: ignore` is django-stubs, not a real problem: `current_severity_band` is an
        # annotation added by `annotate_queue_fields()` above, and the plugin only resolves lookups
        # against declared model fields — it cannot see an alias introduced in another function.
        queryset = queryset.filter(current_severity_band__in=severities)  # type: ignore[misc]

    if statuses:
        queryset = queryset.filter(status__in=statuses)

    if assigned_to_me:
        # ⚠️ The anonymous branch is unreachable through HTTP — the query serializer answers `400` for
        # `assignedTo=me` without a session, because an empty page would read as "you have no work"
        # rather than "you are not signed in". It fails closed anyway: a non-HTTP caller must not get
        # every Issue back from a filter it asked to have applied.
        queryset = queryset.filter(assignee=actor) if isinstance(actor, User) else queryset.none()

    if opened_after is not None:
        queryset = queryset.filter(opened_at__gt=opened_after)

    if bbox is not None:
        # ⚠️ **`bboverlaps` (`&&`), not `__within`.** `ST_Within` is not defined for `geography`, so
        # `__within` on this column either errors or forces a cast that abandons the GiST index. For
        # a *point* left operand against a rectangle, `&&` is exact rather than an approximation —
        # the bounding box of a point is the point — so nothing is lost by using the index operator.
        queryset = queryset.filter(representative_location__bboverlaps=bbox)

    if near is not None and radius_m is not None:
        # `__dwithin` on the `geography` column takes **metres** (T2.1); on a `geometry(4326)` column
        # the same lookup would read `radiusM` as *degrees*. `Distance(m=...)` states the unit here.
        queryset = queryset.filter(representative_location__dwithin=(near, Distance(m=radius_m)))

    if query:
        # ⚠️ **`Exists()` over member Reports, never a join.** A join emits one output row per
        # matching Report, and duplicate rows break both the keyset cursor (two rows share a
        # position) and `meta.count`. `Exists` stays one row per Issue by construction.
        #
        # ⚠️ **Moderated Reports are excluded from the subquery**, or `?q=<phrase from a hidden
        # report>` confirms that suppressed text exists — the FR-31 leak, through a filter rather
        # than a body.
        #
        # `icontains` is the honest limit `list_reports()` documents: PostgreSQL ships no Bangla
        # text-search configuration, so a `SearchVector` would stem English and do nothing for Bangla
        # while looking like full-text search.
        queryset = queryset.filter(
            Exists(
                Report.objects.filter(
                    Q(description__icontains=query) | Q(address__icontains=query),
                    issue=OuterRef("pk"),
                ).exclude(status__in=MODERATED_REPORT_STATUSES)
            )
        )

    return queryset


def attach_proximity(issues: Sequence[Issue]) -> None:
    """Populate `PROXIMITY_ATTR` on each Issue with its nearby POIs (§6.5 `proximity[]`, FR-17).

    ⚠️ **Call this *after* pagination, over the page's rows only.** Two reasons, and the first is a
    constraint rather than an optimisation: C-10 makes POI proximity display-only, so it must not be
    reachable from anything that filters or orders the collection — attaching it to the queryset would
    put it exactly there. The second is cost: one short GiST lookup per row is fine for the ≤100 rows a
    page can hold and is not fine for the collection.

    ⚠️ **One query per row, knowingly.** A single query returning the nearest three POIs *per Issue*
    needs a lateral join, which the ORM cannot express without raw SQL; `LIMIT 3` is per-Issue, so no
    `IN (...)` form is equivalent. The bound is the page size, which is the point of the previous
    paragraph — the N+1 test asserts proximity's cost is bounded by the page, not that it is absent.

    ⚠️ **A non-positive setting raises rather than degrading.** `nearby_pois()` rejects a
    `radius_m`/`limit` of zero, and that `ValueError` is deliberately allowed to propagate: a
    misconfigured deployment should fail loudly instead of silently serving `proximity: []` on every
    row — the `active_city_boundary()` posture (fail closed, never a falsy default).

    Mutates in place and returns `None` rather than a new list, so the caller keeps the *paginator's*
    row objects — the ones the serializer will render.
    """
    radius_m = float(settings.ISSUE_PROXIMITY_RADIUS_M)
    limit = int(settings.ISSUE_PROXIMITY_LIMIT)

    for issue in issues:
        setattr(
            issue,
            PROXIMITY_ATTR,
            list(nearby_pois(point=issue.representative_location, radius_m=radius_m, limit=limit)),
        )
