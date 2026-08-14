"""`factory_boy` factories for `geo` (T2.1).

`testing.md` mandates `factory_boy`; T2.1 owns the first factories because it ships the first
models a test needs to build more than one of. The P4 concurrency test's `_seed_*` helpers
(`urbenmend/issues/tests/test_clustering_concurrency.py`) are the placeholders these replace —
they stay until T4.2 lands `Issue`, since the report they build has fields that do not exist yet.
"""

from __future__ import annotations

import factory
from django.contrib.gis.geos import MultiPolygon, Point, Polygon

from urbenmend.geo.models import POI, CityBoundary, POIType

# A small square around central Dhaka, well inside `docs/city-boundary/dhaka-demo.geojson`.
# ⚠️ **Deliberately smaller than the seeded stand-in.** A factory-built boundary must not be
# mistakeable for the real served city, and a test asserting containment needs a point it can
# place *outside* without leaving the plausible-coordinates range.
_CENTRE = (90.4125, 23.8103)
_HALF_SIDE = 0.02

# A point outside `_square()` but still in Bangladesh — the BR-35 rejection case. Chosen so the
# failure is "outside the boundary", not "not a coordinate on Earth"; a `(0, 0)` control would
# also pass an out-of-city test while proving far less about the query.
OUTSIDE_POINT = Point(90.6000, 24.1000, srid=4326)
INSIDE_POINT = Point(*_CENTRE, srid=4326)


def square(centre: tuple[float, float] = _CENTRE, half_side: float = _HALF_SIDE) -> MultiPolygon:
    """An axis-aligned square as a one-ring `MultiPolygon`, SRID 4326.

    ⚠️ **`MultiPolygon`, not `Polygon`** — `CityBoundary.area` is a `MultiPolygonField`, and
    assigning a bare `Polygon` fails at save with a type error rather than at build time.
    """
    lng, lat = centre
    ring = Polygon(
        (
            (lng - half_side, lat - half_side),
            (lng + half_side, lat - half_side),
            (lng + half_side, lat + half_side),
            (lng - half_side, lat + half_side),
            (lng - half_side, lat - half_side),
        ),
        srid=4326,
    )
    return MultiPolygon(ring, srid=4326)


class CityBoundaryFactory(factory.django.DjangoModelFactory[CityBoundary]):
    """A served-city boundary.

    ⚠️ **`is_active` defaults to `False`, unlike the model.** The migration already seeds one
    active boundary, and `active_city_boundary()` raises when two are active — a factory
    defaulting to `True` would break every test that merely needs *a* boundary row, and the
    failure would look like a selector bug. Tests that want the active one pass
    `is_active=True` after retiring the seeded row, or use the `active_boundary` fixture.
    """

    class Meta:
        model = CityBoundary

    # Sequenced because `name` is UNIQUE — a constant collides on the second build, and the
    # `IntegrityError` surfaces inside whichever test happens to build two.
    name = factory.Sequence(lambda n: f"Test boundary {n}")
    area = factory.LazyFunction(square)
    is_active = False


class POIFactory(factory.django.DjangoModelFactory[POI]):
    """A display-only POI near central Dhaka."""

    class Meta:
        model = POI

    name = factory.Sequence(lambda n: f"Test POI {n}")
    poi_type = POIType.HOSPITAL
    location = factory.LazyFunction(lambda: INSIDE_POINT.clone())
    source = "test"
    is_active = True
