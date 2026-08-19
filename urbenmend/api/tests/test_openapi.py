from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_openapi_schema_is_public_and_machine_readable() -> None:
    response = APIClient().get(reverse("openapi-schema"), HTTP_ACCEPT="application/json")

    assert response.status_code == 200
    body = response.json()
    assert body["openapi"].startswith("3.")
    assert body["info"]["title"] == "UrbanMend API"
    assert "/api/v1/auth/password/forgot" in body["paths"]


def test_swagger_ui_is_public_and_points_to_the_schema() -> None:
    response = APIClient().get(reverse("swagger-ui"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "swagger-ui" in content.lower()
    assert reverse("openapi-schema") in content
