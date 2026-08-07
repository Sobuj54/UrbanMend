"""T2.1 — `CityBoundary` and the boundary selectors (BR-35, C-11).

Covers the model's shape guarantees, the seeded stand-in, and `active_city_boundary()` /
`is_within_city()`. The *intake* consequence — a `422 OUT_OF_CITY` from `create_report()` — is
asserted in `reporting/tests/test_services.py`; this module asserts the primitive underneath it.

[doc: testing.md "out-of-city rejection (C-11)"; data-model §16]
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.db import IntegrityError

from urbenmend.geo.models import CityBoundary
from urbenmend.geo.selectors import BoundaryUnavailable, active_city_boundary, is_within_city
from urbenmend.geo.tests.factories import (
    INSIDE_POINT,
    OUTSIDE_POINT,
    CityBoundaryFactory,
    square,
)

pytestmark = pytest.mark.django_db

SEEDED_NAME = "Dhaka (development stand-in)"


# ---------------------------------------------------------------------------------------
# The seeded stand-in (geo/0002_seed_city_boundary)
# ---------------------------------------------------------------------------------------
def test_migration_seeds_exactly_one_active_boundary() -> None:
    """The seed migration must leave the system in the state `active_city_boundary()` requires.

    ⚠️ This is the test that fails if someone adds a second seeded boundary, or flips the
    stand-in inactive "because it is not real". Either would make every `POST /reports` raise
    `BoundaryUnavailable` — and the failure would surface as a `500` on submission, far from the
    migration that caused it.
    """
    assert CityBoundary.objects.filter(is_active=True).count() == 1
    assert CityBoundary.objects.get(is_active=True).name == SEEDED_NAME


def test_seeded_boundary_is_a_multipolygon_in_4326() -> None:
    """SRID and geometry type must match `Report.location`'s side of the comparison.

    A boundary loaded in a projected SRID would still store and still index; `ST_Within` would
    then compare metres against degrees and reject the whole city.
    """
    boundary = CityBoundary.objects.get(name=SEEDED_NAME)
    assert boundary.area.srid == 4326
    assert boundary.area.geom_type == "MultiPolygon"
    assert not boundary.area.empty


def test_seeded_boundary_contains_central_dhaka() -> None:
    """The stand-in is a stand-in, but it must actually be Dhaka-shaped.

    Without this, a polygon with transposed lat/lng would pass every structural check above and
    silently reject every real submission — the classic 4326 ordering mistake (GeoJSON is
    `[lng, lat]`).
    """
    assert is_within_city(Point(90.4125, 23.8103, srid=4326)) is True


# ---------------------------------------------------------------------------------------
# active_city_boundary()
# ---------------------------------------------------------------------------------------
def test_active_city_boundary_returns_the_active_row() -> None:
    assert active_city_boundary().name == SEEDED_NAME


def test_active_city_boundary_ignores_retired_rows() -> None:
    """A retired boundary is history, not a candidate (database.md: retire, never delete)."""
    CityBoundaryFactory.create(is_active=False)

    assert active_city_boundary().name == SEEDED_NAME


def test_active_city_boundary_raises_when_none_is_active() -> None:
    """⚠️ **Raises rather than returning `None`** — the fail-closed contract.

    A `None` return invites `if boundary and not contains(...)`, which accepts every location on
    Earth the moment the boundary table is empty. Arch §409 sanctions degrading when a dependency
    is missing; C-11 says an out-of-city location "is not accepted", so this path does not take
    that degradation.
    """
    CityBoundary.objects.update(is_active=False)

    with pytest.raises(BoundaryUnavailable):
        active_city_boundary()


def test_active_city_boundary_raises_when_two_are_active() -> None:
    """Ambiguity surfaces as an error, never a silent pick.

    Exactly-one-active is deliberately not a DB constraint (see `CityBoundary`), so this
    selector is the only thing standing between a half-finished boundary swap and reports being
    validated against whichever row sorted first.
    """
    CityBoundaryFactory.create(is_active=True)

    with pytest.raises(BoundaryUnavailable):
        active_city_boundary()


# ---------------------------------------------------------------------------------------
# is_within_city()
# ---------------------------------------------------------------------------------------
def test_point_inside_the_boundary_is_within_the_city() -> None:
    CityBoundary.objects.update(is_active=False)
    CityBoundaryFactory.create(is_active=True)

    assert is_within_city(INSIDE_POINT) is True


def test_point_outside_the_boundary_is_not_within_the_city() -> None:
    """BR-35's rejection case — a real coordinate that is simply not in the served city."""
    CityBoundary.objects.update(is_active=False)
    CityBoundaryFactory.create(is_active=True)

    assert is_within_city(OUTSIDE_POINT) is False


def test_containment_ignores_retired_boundaries() -> None:
    """A point inside a *retired* boundary is out of city.

    ⚠️ The consequence of getting this wrong is silent and one-directional: every historical
    boundary would widen the accepted area forever, and the city could never be shrunk.
    """
    CityBoundary.objects.update(is_active=False)
    retired = CityBoundaryFactory.create(is_active=False)
    centroid = retired.area.centroid
    inside_retired = Point(centroid.x, centroid.y, srid=4326)
    CityBoundaryFactory.create(is_active=True, area=square(centre=(91.0, 24.5)))

    assert is_within_city(inside_retired) is False


def test_containment_with_no_active_boundary_is_false_not_true() -> None:
    """⚠️ Fails **closed**: an empty boundary table accepts nothing.

    `is_within_city()` is a plain `.exists()`, so this holds structurally — but the assertion is
    here because the tempting "optimization" (skip the filter when no boundary is configured)
    inverts it, and every other test in this file would still pass.
    """
    CityBoundary.objects.update(is_active=False)

    assert is_within_city(INSIDE_POINT) is False


# ---------------------------------------------------------------------------------------
# Model shape
# ---------------------------------------------------------------------------------------
def test_boundary_name_is_unique() -> None:
    with pytest.raises(IntegrityError):
        CityBoundaryFactory.create(name=SEEDED_NAME)


def test_area_rejects_a_bare_polygon() -> None:
    """⚠️ `MultiPolygonField`, not `PolygonField` — real boundaries are discontiguous.

    Asserted because the failure mode is late: a `Polygon` assigned in Python raises only at
    save, which in production is the moment an operator imports the authoritative city outline.
    """
    ring = Polygon(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)), srid=4326)

    with pytest.raises((ValueError, TypeError)):
        CityBoundaryFactory.create(area=ring)


def test_a_discontiguous_boundary_is_storable() -> None:
    """The reason for `MultiPolygonField`: enclaves and river islands are one boundary."""
    mainland = square(centre=(90.40, 23.80))
    island = square(centre=(90.60, 23.90))
    multi = MultiPolygon(mainland[0], island[0], srid=4326)

    boundary = CityBoundaryFactory.create(area=multi, is_active=False)
    boundary.refresh_from_db()

    assert boundary.area.num_geom == 2


def test_str_is_the_name() -> None:
    assert str(CityBoundary.objects.get(name=SEEDED_NAME)) == SEEDED_NAME
