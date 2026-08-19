from __future__ import annotations
import pytest
from rest_framework.test import APIClient
from django.urls import reverse
from urbenmend.audit.models import AuditEvent
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.identity.tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db

def _client(user=None):
    client = APIClient()
    if user: client.force_authenticate(user)
    return client

def test_categories_are_public_and_use_reference_shape() -> None:
    Category.objects.create(slug="public-test", name_en="Public Test", name_bn="পরীক্ষা")
    response = _client().get(reverse("api:categories"))
    assert response.status_code == 200
    assert {"key": "public-test", "label": {"en": "Public Test", "bn": "পরীক্ষা"}, "active": True} in response.data

def test_admin_creates_and_audits_category_but_citizen_cannot() -> None:
    url = reverse("api:categories")
    body = {"key": "street-lights", "label": {"en": "Street Lights", "bn": "সড়ক বাতি"}}
    assert _client(UserFactory()).post(url, body, format="json").status_code == 403
    response = _client(AdminFactory()).post(url, body, format="json")
    assert response.status_code == 201
    category = Category.objects.get(slug="street-lights")
    assert AuditEvent.objects.get(action="reference.category_created").target == category

def test_duplicate_key_is_conflict() -> None:
    Category.objects.create(slug="duplicate-test", name_en="Duplicate Test", name_bn="এক")
    response = _client(AdminFactory()).post(reverse("api:categories"),
        {"key": "duplicate-test", "label": {"en": "Different", "bn": "দুই"}}, format="json")
    assert response.status_code == 409

def test_patch_retires_without_changing_key_or_deleting() -> None:
    category = Category.objects.create(slug="retire-test", name_en="Retire Test", name_bn="পুরনো")
    response = _client(AdminFactory()).patch(reverse("api:categories-detail", kwargs={"key": category.slug}),
        {"label": {"en": "Retired Label", "bn": "অবসর"}, "active": False, "key": "changed"}, format="json")
    assert response.status_code == 400
    response = _client(AdminFactory()).patch(reverse("api:categories-detail", kwargs={"key": category.slug}),
        {"label": {"en": "Retired Label", "bn": "অবসর"}, "active": False}, format="json")
    assert response.status_code == 200
    category.refresh_from_db()
    assert category.slug == "retire-test"
    assert category.status == CategoryStatus.RETIRED
    event = AuditEvent.objects.get(action="reference.category_updated")
    assert event.before["active"] is True and event.after["active"] is False
