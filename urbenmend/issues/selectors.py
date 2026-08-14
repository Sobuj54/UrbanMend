"""Geospatial Issue read primitives (T4.2).

These functions deliberately express spatial work through GeoDjango so PostGIS can use the
`issues_issue_location_gist` index. Category/time-window matching and open-status rules belong to
T4.3/T4.4; public/authority queue visibility belongs to the later Issue read endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.gis.db.models.functions import GeometryDistance
from django.contrib.gis.measure import Distance

from urbenmend.issues.models import ClusteringRule, ClusteringRuleStatus, Issue

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point
    from django.db.models import QuerySet


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


def issues_within_radius(*, point: Point, radius_m: float) -> QuerySet[Issue]:
    """Issues within `radius_m` metres of `point`, using index-assisted `ST_DWithin`.

    `representative_location` is PostGIS `geography(Point, 4326)`, so the lookup's distance unit is
    metres. The explicit `Distance` value keeps that unit visible at the call site.
    """
    return Issue.objects.filter(representative_location__dwithin=(point, Distance(m=radius_m)))


def nearest_issues(*, point: Point) -> QuerySet[Issue]:
    """Issues ordered nearest-first using PostGIS's GiST-assisted KNN `<->` operator.

    Callers apply their own visibility filters and slice the lazy queryset to the required limit.
    The UUID tie-break makes equidistant rows deterministic without replacing the KNN ordering.
    """
    return Issue.objects.annotate(
        knn_distance=GeometryDistance("representative_location", point)
    ).order_by("knn_distance", "pk")
