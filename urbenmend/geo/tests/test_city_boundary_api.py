import pytest
from django.urls import reverse
from rest_framework.test import APIClient
from urbenmend.audit.models import AuditEvent
from urbenmend.geo.models import CityBoundary
from urbenmend.geo.selectors import active_city_boundary, is_within_city
from urbenmend.identity.tests.factories import AdminFactory, UserFactory
from django.contrib.gis.geos import Point

pytestmark = pytest.mark.django_db
def client(user=None):
    c=APIClient()
    if user: c.force_authenticate(user)
    return c

GEOMETRY={"type":"Polygon","coordinates":[[[90.40,23.80],[90.42,23.80],[90.42,23.82],[90.40,23.82],[90.40,23.80]]]}

def test_active_boundary_is_public_geojson_feature() -> None:
    response=client().get(reverse("api:city-boundary"))
    assert response.status_code == 200
    assert response.data["type"] == "Feature"
    assert response.data["geometry"]["type"] == "MultiPolygon"
    assert response.data["properties"]["active"] is True

def test_admin_replacement_retires_old_normalizes_polygon_and_audits() -> None:
    old=active_city_boundary(); url=reverse("api:city-boundary")
    assert client(UserFactory()).put(url, {"name":"New boundary","geometry":GEOMETRY}, format="json").status_code == 403
    response=client(AdminFactory()).put(url, {"name":"New boundary","geometry":GEOMETRY}, format="json")
    assert response.status_code == 200
    old.refresh_from_db(); new=active_city_boundary()
    assert old.is_active is False and new.name == "New boundary"
    assert new.area.geom_type == "MultiPolygon"
    assert is_within_city(Point(90.41,23.81,srid=4326)) is True
    event=AuditEvent.objects.get(action="reference.city_boundary_replaced")
    assert event.before["id"] == str(old.pk) and event.target == new

def test_invalid_geometry_duplicate_name_and_unknown_fields_are_rejected() -> None:
    admin=AdminFactory(); url=reverse("api:city-boundary")
    assert client(admin).put(url, {"name":"Bad","geometry":{"type":"Point","coordinates":[90,23]}}, format="json").status_code == 400
    existing=active_city_boundary()
    assert client(admin).put(url, {"name":existing.name,"geometry":GEOMETRY}, format="json").status_code == 409
    assert client(admin).put(url, {"name":"Extra","geometry":GEOMETRY,"unexpected":1}, format="json").status_code == 400
    assert CityBoundary.objects.filter(is_active=True).count() == 1

def test_replacement_fails_atomically_when_active_state_is_invalid() -> None:
    CityBoundary.objects.update(is_active=False)
    response=client(AdminFactory()).put(reverse("api:city-boundary"), {"name":"Cannot replace","geometry":GEOMETRY}, format="json")
    assert response.status_code == 409
    assert not CityBoundary.objects.filter(name="Cannot replace").exists()
