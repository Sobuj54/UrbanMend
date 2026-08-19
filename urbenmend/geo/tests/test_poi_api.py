import pytest
from django.contrib.gis.geos import Point
from django.urls import reverse
from rest_framework.test import APIClient
from urbenmend.audit.models import AuditEvent
from urbenmend.geo.models import POI
from urbenmend.geo.tests.factories import POIFactory
from urbenmend.identity.tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db
def client(user=None):
    c=APIClient()
    if user: c.force_authenticate(user)
    return c

def test_reads_require_session_and_filter_and_paginate() -> None:
    POIFactory(name="Hospital A", poi_type="hospital"); POIFactory(name="School", poi_type="school")
    url=reverse("api:pois")
    assert client().get(url).status_code == 401
    response=client(UserFactory()).get(url, {"type":"hospital", "limit":1})
    assert response.status_code == 200 and response.data["meta"]["count"] == 1
    assert response.data["data"][0]["type"] == "hospital"

def test_admin_create_update_and_retire_are_audited() -> None:
    admin=AdminFactory(); url=reverse("api:pois")
    body={"name":"Clinic", "type":"hospital", "location":{"lng":90.41,"lat":23.81}, "source":"osm"}
    assert client(UserFactory()).post(url, body, format="json").status_code == 403
    created=client(admin).post(url, body, format="json"); assert created.status_code == 201
    poi=POI.objects.get(pk=created.data["id"])
    detail=reverse("api:pois-detail", kwargs={"poi_id":poi.pk})
    assert client(admin).patch(detail, {"name":"Clinic 2"}, format="json").status_code == 200
    assert client(admin).delete(detail).status_code == 200
    poi.refresh_from_db(); assert poi.is_active is False
    assert set(AuditEvent.objects.values_list("action", flat=True)) == {"reference.poi_created", "reference.poi_updated"}

def test_location_is_immutable_after_creation_and_spatial_params_validate() -> None:
    poi=POIFactory(); admin=AdminFactory(); detail=reverse("api:pois-detail", kwargs={"poi_id":poi.pk})
    assert client(admin).patch(detail, {"location":{"lng":1,"lat":2}}, format="json").status_code == 400
    url=reverse("api:pois")
    assert client(admin).get(url, {"nearLng":90.4}).status_code == 400
    assert client(admin).get(url, {"bbox":"bad"}).status_code == 400

def test_bbox_and_radius_filters_execute_in_postgis() -> None:
    user=UserFactory()
    inside=POIFactory(location=Point(90.4125, 23.8103, srid=4326))
    POIFactory(location=Point(90.60, 24.10, srid=4326))
    url=reverse("api:pois")
    bbox=client(user).get(url, {"bbox":"90.40,23.80,90.42,23.82"})
    assert bbox.status_code == 200
    assert [item["id"] for item in bbox.data["data"]] == [str(inside.pk)]
    nearby=client(user).get(url, {"nearLng":90.4125, "nearLat":23.8103, "radiusM":100})
    assert nearby.status_code == 200
    assert [item["id"] for item in nearby.data["data"]] == [str(inside.pk)]
