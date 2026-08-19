import pytest
from django.urls import reverse
from rest_framework.test import APIClient
from urbenmend.audit.models import AuditEvent
from urbenmend.classification.models import Category
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory
from urbenmend.issues.models import ClusteringRule, ClusteringRuleStatus

pytestmark = pytest.mark.django_db
def client(user=None):
    c=APIClient()
    if user: c.force_authenticate(user)
    return c

def test_list_is_admin_only_and_paginated() -> None:
    url=reverse("api:clustering-rules")
    assert client().get(url).status_code == 401
    assert client(AuthorityFactory()).get(url).status_code == 403
    response=client(AdminFactory()).get(url, {"limit":2})
    assert response.status_code == 200 and len(response.data["data"]) == 2

def test_create_conflicts_until_existing_rule_is_retired_then_audits() -> None:
    category=Category.objects.get(slug="roads")
    existing=ClusteringRule.objects.get(category=category, status=ClusteringRuleStatus.ACTIVE)
    collection=reverse("api:clustering-rules"); admin=AdminFactory()
    body={"category":"roads", "radiusM":75, "timeWindowHours":48}
    assert client(admin).post(collection, body, format="json").status_code == 409
    detail=reverse("api:clustering-rules-detail", kwargs={"rule_id":existing.pk})
    assert client(admin).patch(detail, {"active":False}, format="json").status_code == 200
    created=client(admin).post(collection, body, format="json")
    assert created.status_code == 201
    rule=ClusteringRule.objects.get(pk=created.data["id"])
    assert rule.radius_m == 75 and rule.time_window_hours == 48
    assert set(AuditEvent.objects.values_list("action", flat=True)) == {"reference.clustering_rule_updated", "reference.clustering_rule_created"}

def test_positive_bounds_category_validation_and_category_immutability() -> None:
    admin=AdminFactory(); collection=reverse("api:clustering-rules")
    assert client(admin).post(collection, {"category":"missing", "radiusM":1, "timeWindowHours":1}, format="json").status_code == 404
    assert client(admin).post(collection, {"category":"roads", "radiusM":0, "timeWindowHours":1}, format="json").status_code == 400
    rule=ClusteringRule.objects.first(); detail=reverse("api:clustering-rules-detail", kwargs={"rule_id":rule.pk})
    assert client(admin).patch(detail, {"category":"electrical"}, format="json").status_code == 400

def test_tuning_active_rule_changes_selector_input() -> None:
    rule=ClusteringRule.objects.filter(status=ClusteringRuleStatus.ACTIVE).first()
    response=client(AdminFactory()).patch(reverse("api:clustering-rules-detail", kwargs={"rule_id":rule.pk}),
        {"radiusM":35, "timeWindowHours":24}, format="json")
    assert response.status_code == 200
    rule.refresh_from_db(); assert (rule.radius_m, rule.time_window_hours) == (35,24)
