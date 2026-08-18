"""Analytics summary endpoint contract (T7.5)."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from urbenmend.classification.models import Category
from urbenmend.identity.models import Role
from urbenmend.identity.tests.factories import AuthorityFactory, UserFactory
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def test_analytics_requires_authority_or_admin(client: Client) -> None:
    response = client.get(reverse("api:analytics-summary"))

    assert response.status_code == 401


def test_analytics_returns_grouped_counts_for_authority(client: Client) -> None:
    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug="roads"))
    IssueFactory.create()
    IssueFactory.create()
    client.force_login(authority)

    response = client.get(reverse("api:analytics-summary"), {"groupBy": "severity"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["groupBy"] == "severity"
    assert payload["groups"] == [{"key": "medium", "count": 2}]
    assert payload["metrics"]["total"] == 2
    assert payload["metrics"]["open"] == 2
    assert payload["metrics"]["resolved"] == 0


def test_analytics_applies_authority_scope(client: Client) -> None:
    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug="roads"))
    IssueFactory.create()
    IssueFactory.create(primary_category=Category.objects.get(slug="water_drainage"))
    client.force_login(authority)

    response = client.get(reverse("api:analytics-summary"))

    assert response.status_code == 200
    assert response.json()["metrics"]["total"] == 1


def test_analytics_rejects_citizen_and_invalid_date_range(client: Client) -> None:
    citizen = UserFactory.create(role=Role.CITIZEN)
    client.force_login(citizen)
    assert client.get(reverse("api:analytics-summary")).status_code == 403

    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug="roads"))
    client.force_login(authority)
    response = client.get(
        reverse("api:analytics-summary"),
        {"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "from"
