"""Real-PostGIS tests for the T4.2 radius and nearest-Issue query primitives."""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.db import connection

from urbenmend.issues.selectors import issues_within_radius, nearest_issues
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db

CENTRE = Point(90.4125, 23.8103, srid=4326)
NEAR = Point(90.4130, 23.8103, srid=4326)
FAR = Point(90.55, 23.8103, srid=4326)


def test_radius_query_uses_geography_metres() -> None:
    near = IssueFactory.create(representative_location=NEAR)
    IssueFactory.create(representative_location=FAR)

    found = list(issues_within_radius(point=CENTRE, radius_m=1000))

    assert found == [near]


def test_radius_query_compiles_to_st_dwithin() -> None:
    query = str(issues_within_radius(point=CENTRE, radius_m=1000).query)

    assert "ST_DWithin" in query


def test_nearest_query_orders_by_real_distance() -> None:
    far = IssueFactory.create(representative_location=FAR)
    nearest = IssueFactory.create(representative_location=CENTRE)
    near = IssueFactory.create(representative_location=NEAR)

    ordered_ids = list(nearest_issues(point=CENTRE).values_list("pk", flat=True))

    assert ordered_ids == [nearest.pk, near.pk, far.pk]


def test_nearest_query_compiles_to_the_postgis_knn_operator() -> None:
    query = str(nearest_issues(point=CENTRE).query)

    assert "<->" in query


def test_issue_location_has_one_named_gist_index_in_postgres() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'issues_issue'
              AND indexname = 'issues_issue_location_gist'
            """
        )
        row = cursor.fetchone()

    assert row is not None
    index_definition = row[0]
    assert "USING gist" in index_definition
    assert "representative_location" in index_definition


def test_radius_query_plan_can_use_the_named_gist_index() -> None:
    IssueFactory.create(representative_location=NEAR)

    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
    plan = issues_within_radius(point=CENTRE, radius_m=1000).explain()

    assert "issues_issue_location_gist" in plan


def test_knn_query_plan_can_use_the_named_gist_index() -> None:
    IssueFactory.create(representative_location=NEAR)

    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
    plan = nearest_issues(point=CENTRE)[:5].explain()

    assert "issues_issue_location_gist" in plan
