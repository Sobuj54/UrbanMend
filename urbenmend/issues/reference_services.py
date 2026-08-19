from django.db import transaction
from django.http import Http404
from urbenmend.api.exceptions import Conflict
from urbenmend.audit.services import record_event
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role
from urbenmend.issues.models import ClusteringRule, ClusteringRuleStatus

def _active_category(slug: str) -> Category:
    try: return Category.objects.get(slug=slug, status=CategoryStatus.ACTIVE)
    except Category.DoesNotExist as exc: raise Http404("Active category not found.") from exc

@transaction.atomic
def create_clustering_rule(*, actor: User, category: str, radius_m: int, time_window_hours: int) -> ClusteringRule:
    require_role(actor, Role.ADMIN)
    category_obj = _active_category(category)
    if ClusteringRule.objects.filter(category=category_obj, status=ClusteringRuleStatus.ACTIVE).exists():
        raise Conflict("This category already has an active clustering rule.", code="ACTIVE_RULE_EXISTS")
    rule = ClusteringRule.objects.create(category=category_obj, radius_m=radius_m, time_window_hours=time_window_hours)
    record_event(actor=actor, action="reference.clustering_rule_created", target=rule,
        after={"category": category, "radius_m": radius_m, "time_window_hours": time_window_hours, "active": True})
    return rule

@transaction.atomic
def update_clustering_rule(*, actor: User, rule_id: int, **changes) -> ClusteringRule:
    require_role(actor, Role.ADMIN)
    try: rule = ClusteringRule.objects.select_for_update().get(pk=rule_id)
    except ClusteringRule.DoesNotExist as exc: raise Http404("Clustering rule not found.") from exc
    if "category" in changes: raise ValueError("A clustering rule's category is immutable.")
    before = {"category": rule.category.slug, "radius_m": rule.radius_m, "time_window_hours": rule.time_window_hours, "active": rule.status == ClusteringRuleStatus.ACTIVE}
    if "active" in changes:
        active = changes.pop("active")
        if active and ClusteringRule.objects.exclude(pk=rule.pk).filter(category=rule.category, status=ClusteringRuleStatus.ACTIVE).exists():
            raise Conflict("This category already has an active clustering rule.", code="ACTIVE_RULE_EXISTS")
        rule.status = ClusteringRuleStatus.ACTIVE if active else ClusteringRuleStatus.RETIRED
    for key, value in changes.items(): setattr(rule, key, value)
    rule.save()
    after = {"category": rule.category.slug, "radius_m": rule.radius_m, "time_window_hours": rule.time_window_hours, "active": rule.status == ClusteringRuleStatus.ACTIVE}
    record_event(actor=actor, action="reference.clustering_rule_updated", target=rule, before=before, after=after)
    return rule
