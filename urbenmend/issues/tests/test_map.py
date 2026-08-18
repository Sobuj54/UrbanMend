"""GeoJSON map endpoint contract and low-zoom aggregation (T7.4)."""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.test import Client
from django.urls import reverse

from urbenmend.classification.models import Category
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.tests.factories import ConfirmationFactory, IssueFactory

pytestmark = pytest.mark.django_db

MAP_BBOX = "90.40,23.80,90.43,23.83"


def _url(**params: object) -> str:
    query = {"bbox": MAP_BBOX, **params}
    return f"{reverse('api:map-issues')}?" + "&".join(
        f"{key}={value}" for key, value in query.items()
    )


def test_bbox_is_required(client: Client) -> None:
    response = client.get(reverse("api:map-issues"))

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "bbox"


@pytest.mark.parametrize("zoom", ["close", -1, 23])
def test_zoom_must_be_a_supported_integer(client: Client, zoom: object) -> None:
    response = client.get(_url(zoom=zoom))

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "zoom"


def test_high_zoom_returns_individual_geojson_points(client: Client) -> None:
    issue = IssueFactory.create(representative_location=Point(90.4125, 23.8103, srid=4326))
    ConfirmationFactory.create(issue=issue)

    response = client.get(_url(zoom=12))

    assert response.status_code == 200
    assert response.json() == {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": str(issue.pk),
                "geometry": {"type": "Point", "coordinates": [90.4125, 23.8103]},
                "properties": {
                    "severity": "medium",
                    "status": "submitted",
                    "corroborationCount": 1,
                    "count": 1,
                },
            }
        ],
    }


def test_map_filters_and_excludes_moderated_issues(client: Client) -> None:
    IssueFactory.create()
    IssueFactory.create(status=IssueStatus.HIDDEN)
    drainage = Category.objects.get(slug="water_drainage")
    included = IssueFactory.create(primary_category=drainage)

    response = client.get(_url(category="water_drainage", zoom=12))

    assert response.status_code == 200
    assert [feature["id"] for feature in response.json()["features"]] == [str(included.pk)]


def test_low_zoom_aggregates_issues_and_corroborations(client: Client) -> None:
    first = IssueFactory.create(representative_location=Point(90.4125, 23.8103, srid=4326))
    second = IssueFactory.create(representative_location=Point(90.4126, 23.8104, srid=4326))
    ConfirmationFactory.create(issue=first)
    ConfirmationFactory.create(issue=second)
    ConfirmationFactory.create(issue=second)

    response = client.get(_url(zoom=8))

    assert response.status_code == 200
    features = response.json()["features"]
    assert len(features) == 1
    assert features[0]["id"].startswith("cluster-8-")
    assert features[0]["properties"] == {"count": 2, "corroborationCount": 3}
    assert features[0]["geometry"]["coordinates"] == pytest.approx([90.41255, 23.81035])


def test_unknown_queue_parameter_is_rejected(client: Client) -> None:
    response = client.get(_url(sort="severity"))

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "sort"
