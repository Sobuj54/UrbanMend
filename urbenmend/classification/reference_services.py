from __future__ import annotations

from django.db import transaction
from django.http import Http404

from urbenmend.api.exceptions import Conflict
from urbenmend.audit.services import record_event
from urbenmend.classification.keywords import normalize_term
from urbenmend.classification.models import (
    Category,
    CategoryStatus,
    SeverityKeyword,
    SeverityKeywordStatus,
)
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role


@transaction.atomic
def create_category(*, actor: User, key: str, label: dict[str, str]) -> Category:
    require_role(actor, Role.ADMIN)
    if Category.objects.filter(slug=key).exists():
        raise Conflict("A category with this key already exists.", code="DUPLICATE_KEY")
    category = Category.objects.create(
        slug=key, name_en=label["en"].strip(), name_bn=label["bn"].strip()
    )
    record_event(
        actor=actor,
        action="reference.category_created",
        target=category,
        after={"key": category.slug, "label": label, "active": True},
    )
    return category


def _keyword_category(slug: str | None):
    if slug is None:
        return None
    try:
        return Category.objects.get(slug=slug, status=CategoryStatus.ACTIVE)
    except Category.DoesNotExist as exc:
        raise Http404("Active category not found.") from exc


@transaction.atomic
def create_severity_keyword(
    *, actor: User, term: str, language: str, severity: str, category: str | None = None
) -> SeverityKeyword:
    require_role(actor, Role.ADMIN)
    normalized = normalize_term(term)
    if SeverityKeyword.objects.filter(term=normalized).exists():
        raise Conflict("A severity keyword with this term already exists.", code="DUPLICATE_TERM")
    keyword = SeverityKeyword.objects.create(
        term=normalized, language=language, severity=severity, category=_keyword_category(category)
    )
    record_event(
        actor=actor,
        action="reference.severity_keyword_created",
        target=keyword,
        after={
            "term": keyword.term,
            "language": language,
            "severity": severity,
            "category": category,
            "active": True,
        },
    )
    return keyword


@transaction.atomic
def update_severity_keyword(*, actor: User, keyword_id: int, **changes) -> SeverityKeyword:
    require_role(actor, Role.ADMIN)
    try:
        keyword = SeverityKeyword.objects.select_for_update().get(pk=keyword_id)
    except SeverityKeyword.DoesNotExist as exc:
        raise Http404("Severity keyword not found.") from exc
    before = {
        "term": keyword.term,
        "language": keyword.language,
        "severity": keyword.severity,
        "category": keyword.category.slug if keyword.category else None,
        "active": keyword.status == SeverityKeywordStatus.ACTIVE,
    }
    if "term" in changes:
        normalized = normalize_term(changes.pop("term"))
        if SeverityKeyword.objects.exclude(pk=keyword.pk).filter(term=normalized).exists():
            raise Conflict(
                "A severity keyword with this term already exists.", code="DUPLICATE_TERM"
            )
        keyword.term = normalized
    if "category" in changes:
        keyword.category = _keyword_category(changes.pop("category"))
    if "active" in changes:
        keyword.status = (
            SeverityKeywordStatus.ACTIVE if changes.pop("active") else SeverityKeywordStatus.RETIRED
        )
    for key, value in changes.items():
        setattr(keyword, key, value)
    keyword.save()
    after = {
        "term": keyword.term,
        "language": keyword.language,
        "severity": keyword.severity,
        "category": keyword.category.slug if keyword.category else None,
        "active": keyword.status == SeverityKeywordStatus.ACTIVE,
    }
    record_event(
        actor=actor,
        action="reference.severity_keyword_updated",
        target=keyword,
        before=before,
        after=after,
    )
    return keyword


@transaction.atomic
def update_category(
    *, actor: User, key: str, label: dict[str, str] | None, active: bool | None
) -> Category:
    require_role(actor, Role.ADMIN)
    try:
        category = Category.objects.select_for_update().get(slug=key)
    except Category.DoesNotExist as exc:
        raise Http404("Category not found.") from exc
    before = {
        "label": {"en": category.name_en, "bn": category.name_bn},
        "active": category.status == CategoryStatus.ACTIVE,
    }
    if label is not None:
        category.name_en, category.name_bn = label["en"].strip(), label["bn"].strip()
    if active is not None:
        category.status = CategoryStatus.ACTIVE if active else CategoryStatus.RETIRED
    category.save(update_fields=["name_en", "name_bn", "status"])
    after = {
        "label": {"en": category.name_en, "bn": category.name_bn},
        "active": category.status == CategoryStatus.ACTIVE,
    }
    record_event(
        actor=actor,
        action="reference.category_updated",
        target=category,
        before=before,
        after=after,
    )
    return category
