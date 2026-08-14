"""T4.8 - display-only point-of-interest proximity context (FR-17, C-10)."""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.contrib.postgres.indexes import GistIndex
from django.db import connection

from urbenmend.geo.models import POI, POIType
from urbenmend.geo.selectors import nearby_pois
from urbenmend.geo.tests.factories import INSIDE_POINT, POIFactory

pytestmark = pytest.mark.django_db


def _point(*, lng_offset: float = 0.0, lat_offset: float = 0.0) -> Point:
    return Point(INSIDE_POINT.x + lng_offset, INSIDE_POINT.y + lat_offset, srid=4326)


def test_poi_location_is_wgs84_geography_with_named_gist_index() -> None:
    field = POI._meta.get_field("location")
    index = next(index for index in POI._meta.indexes if index.name == "geo_poi_location_gist")

    assert field.srid == 4326
    assert field.geography is True
    assert field.spatial_index is False
    assert isinstance(index, GistIndex)


def test_nearby_pois_returns_active_rows_nearest_first() -> None:
    near = POIFactory.create(name="Near", location=_point(lng_offset=0.0005))
    far = POIFactory.create(name="Far", location=_point(lng_offset=0.0020))
    POIFactory.create(name="Outside", location=_point(lng_offset=0.02))
    POIFactory.create(name="Retired", location=_point(lng_offset=0.0001), is_active=False)

    results = list(nearby_pois(point=INSIDE_POINT, radius_m=500, limit=5))

    assert [poi.pk for poi in results] == [near.pk, far.pk]


def test_nearby_pois_applies_a_stable_limit() -> None:
    pois = [
        POIFactory.create(location=_point(lng_offset=offset)) for offset in (0.0004, 0.0008, 0.0012)
    ]

    first = list(nearby_pois(point=INSIDE_POINT, radius_m=500, limit=2))
    second = list(nearby_pois(point=INSIDE_POINT, radius_m=500, limit=2))

    assert [poi.pk for poi in first] == [pois[0].pk, pois[1].pk]
    assert [poi.pk for poi in second] == [poi.pk for poi in first]


def test_nearby_pois_uses_dwithin_and_knn_sql() -> None:
    sql = str(nearby_pois(point=INSIDE_POINT, radius_m=500, limit=5).query)

    assert "ST_DWithin" in sql
    assert "<->" in sql


@pytest.mark.parametrize(
    ("radius_m", "limit"),
    [(0, 5), (-1, 5), (100, 0), (100, -1)],
)
def test_nearby_pois_rejects_non_positive_bounds(radius_m: float, limit: int) -> None:
    with pytest.raises(ValueError):
        nearby_pois(point=INSIDE_POINT, radius_m=radius_m, limit=limit)


def test_poi_type_vocabulary_is_controlled() -> None:
    assert set(POIType.values) == {"hospital", "school", "highway", "market"}


def test_named_poi_gist_index_exists_in_postgres() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
            [POI._meta.db_table, "geo_poi_location_gist"],
        )
        row = cursor.fetchone()

    assert row is not None
    assert "USING gist" in row[0]
