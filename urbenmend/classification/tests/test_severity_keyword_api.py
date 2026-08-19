from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.audit.models import AuditEvent
from urbenmend.classification.models import Category, SeverityKeyword, SeverityKeywordStatus
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory

pytestmark = pytest.mark.django_db


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def test_list_is_authority_admin_only_and_paginated() -> None:
    url = reverse("api:severity-keywords")
    assert client().get(url).status_code == 401
    assert client(UserFactory()).get(url).status_code == 403
    response = client(AuthorityFactory()).get(url, {"limit": 2})
    assert response.status_code == 200 and len(response.data["data"]) == 2
    assert response.data["page"]["nextCursor"] is not None


def test_admin_create_normalizes_nullable_category_and_audits() -> None:
    url = reverse("api:severity-keywords")
    body = {"term": "  Broken   SIGNAL ", "language": "en", "severity": "high", "category": None}
    assert client(AuthorityFactory()).post(url, body, format="json").status_code == 403
    response = client(AdminFactory()).post(url, body, format="json")
    assert response.status_code == 201
    keyword = SeverityKeyword.objects.get(term="broken signal")
    assert keyword.category is None
    assert AuditEvent.objects.get(action="reference.severity_keyword_created").target == keyword


def test_duplicate_normalized_term_conflicts() -> None:
    SeverityKeyword.objects.create(term="test duplicate", language="en", severity="low")
    response = client(AdminFactory()).post(
        reverse("api:severity-keywords"),
        {"term": " Test  Duplicate ", "language": "bn", "severity": "high"},
        format="json",
    )
    assert response.status_code == 409


def test_patch_and_delete_update_then_retire_without_hard_delete() -> None:
    category = Category.objects.filter(status="active").first()
    keyword = SeverityKeyword.objects.create(
        term="temporary api keyword", language="en", severity="low"
    )
    url = reverse("api:severity-keywords-detail", kwargs={"keyword_id": keyword.pk})
    response = client(AdminFactory()).patch(
        url, {"severity": "medium", "category": category.slug}, format="json"
    )
    assert response.status_code == 200
    assert client(AdminFactory()).delete(url).status_code == 200
    keyword.refresh_from_db()
    assert keyword.severity == "medium" and keyword.category == category
    assert keyword.status == SeverityKeywordStatus.RETIRED
    assert SeverityKeyword.objects.filter(pk=keyword.pk).exists()


def test_create_requires_core_fields_and_active_category() -> None:
    admin = AdminFactory()
    url = reverse("api:severity-keywords")
    assert client(admin).post(url, {"term": "missing fields"}, format="json").status_code == 400
    response = client(admin).post(
        url,
        {
            "term": "unknown category term",
            "language": "en",
            "severity": "low",
            "category": "missing",
        },
        format="json",
    )
    assert response.status_code == 404
