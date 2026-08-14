"""T4.3 per-category clustering configuration, persistence, and admin surface."""

from __future__ import annotations

import importlib

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory

from urbenmend.classification.models import Category
from urbenmend.issues.admin import ClusteringRuleAdmin
from urbenmend.issues.models import ClusteringRule, ClusteringRuleStatus
from urbenmend.issues.selectors import ClusteringRuleUnavailable, active_clustering_rule
from urbenmend.issues.tests.factories import ClusteringRuleFactory

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("urbenmend.issues.migrations.0003_clusteringrule")


def test_migration_seeds_one_active_rule_for_every_category() -> None:
    categories = set(Category.objects.values_list("pk", flat=True))
    rules = ClusteringRule.objects.filter(status=ClusteringRuleStatus.ACTIVE)

    assert set(rules.values_list("category_id", flat=True)) == categories
    assert rules.count() == len(categories)


def test_seed_uses_the_documented_conservative_starting_configuration() -> None:
    assert not ClusteringRule.objects.exclude(
        radius_m=MIGRATION.DEFAULT_RADIUS_M,
        time_window_hours=MIGRATION.DEFAULT_TIME_WINDOW_HOURS,
    ).exists()


def test_radius_and_time_window_must_be_strictly_positive_in_validation() -> None:
    rule = ClusteringRuleFactory.build(radius_m=0, time_window_hours=0)

    with pytest.raises(ValidationError) as caught:
        # The two following tests exercise the database constraints directly. This assertion is
        # specifically for the field validators that power the Django admin form.
        rule.full_clean(validate_constraints=False)

    assert set(caught.value.message_dict) == {"radius_m", "time_window_hours"}


@pytest.mark.parametrize("field", ["radius_m", "time_window_hours"])
def test_radius_and_time_window_are_positive_at_the_database(field: str) -> None:
    roads = Category.objects.get(slug="roads")
    ClusteringRule.objects.filter(category=roads).update(status=ClusteringRuleStatus.RETIRED)
    values = {"radius_m": 50, "time_window_hours": 72, field: 0}

    with pytest.raises(IntegrityError), transaction.atomic():
        ClusteringRule.objects.create(category=roads, **values)


def test_only_one_active_rule_may_exist_per_category() -> None:
    roads = Category.objects.get(slug="roads")

    with pytest.raises(IntegrityError), transaction.atomic():
        ClusteringRuleFactory.create(category=roads)


def test_retired_rule_history_may_coexist_with_the_active_rule() -> None:
    roads = Category.objects.get(slug="roads")

    retired = ClusteringRuleFactory.create(
        category=roads,
        status=ClusteringRuleStatus.RETIRED,
    )

    assert retired.pk is not None
    assert ClusteringRule.objects.filter(category=roads).count() == 2


def test_referenced_categories_are_protected_from_hard_delete() -> None:
    roads = Category.objects.get(slug="roads")

    with pytest.raises(ProtectedError):
        roads.delete()


def test_active_rule_selector_reads_admin_changes_without_a_cache() -> None:
    roads = Category.objects.get(slug="roads")
    rule = active_clustering_rule(category_id=roads.pk)
    rule.radius_m = 35
    rule.time_window_hours = 24
    rule.save(update_fields=["radius_m", "time_window_hours", "updated_at"])

    changed = active_clustering_rule(category_id=roads.pk)

    assert changed.radius_m == 35
    assert changed.time_window_hours == 24


def test_active_rule_selector_fails_closed_when_configuration_is_missing() -> None:
    roads = Category.objects.get(slug="roads")
    ClusteringRule.objects.filter(category=roads).update(status=ClusteringRuleStatus.RETIRED)

    with pytest.raises(ClusteringRuleUnavailable, match=str(roads.pk)):
        active_clustering_rule(category_id=roads.pk)


def test_clustering_rule_admin_is_editable_but_never_hard_deletes() -> None:
    model_admin = admin.site._registry[ClusteringRule]
    request = RequestFactory().get("/admin/issues/clusteringrule/")

    assert isinstance(model_admin, ClusteringRuleAdmin)
    assert model_admin.list_editable == ["radius_m", "time_window_hours", "status"]
    assert model_admin.has_delete_permission(request) is False


def test_string_representation_exposes_the_effective_configuration() -> None:
    roads_rule = ClusteringRule.objects.select_related("category").get(category__slug="roads")

    assert str(roads_rule) == "Roads & Transport: 50 m / 72 h (active)"
