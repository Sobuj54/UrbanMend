from __future__ import annotations
from django.db import transaction
from django.http import Http404
from urbenmend.api.exceptions import Conflict
from urbenmend.audit.services import record_event
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role

@transaction.atomic
def create_category(*, actor: User, key: str, label: dict[str, str]) -> Category:
    require_role(actor, Role.ADMIN)
    if Category.objects.filter(slug=key).exists():
        raise Conflict("A category with this key already exists.", code="DUPLICATE_KEY")
    category = Category.objects.create(slug=key, name_en=label["en"].strip(), name_bn=label["bn"].strip())
    record_event(actor=actor, action="reference.category_created", target=category,
                 after={"key": category.slug, "label": label, "active": True})
    return category

@transaction.atomic
def update_category(*, actor: User, key: str, label: dict[str, str] | None, active: bool | None) -> Category:
    require_role(actor, Role.ADMIN)
    try:
        category = Category.objects.select_for_update().get(slug=key)
    except Category.DoesNotExist as exc:
        raise Http404("Category not found.") from exc
    before = {"label": {"en": category.name_en, "bn": category.name_bn}, "active": category.status == CategoryStatus.ACTIVE}
    if label is not None:
        category.name_en, category.name_bn = label["en"].strip(), label["bn"].strip()
    if active is not None:
        category.status = CategoryStatus.ACTIVE if active else CategoryStatus.RETIRED
    category.save(update_fields=["name_en", "name_bn", "status"])
    after = {"label": {"en": category.name_en, "bn": category.name_bn}, "active": category.status == CategoryStatus.ACTIVE}
    record_event(actor=actor, action="reference.category_updated", target=category, before=before, after=after)
    return category
