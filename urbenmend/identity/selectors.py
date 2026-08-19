"""
Identity & Access — read operations.

Query functions for this module. Kept separate from services.py so reads never acquire
write-path side effects, and so the modules that consume this one have a single documented
surface to call [doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

[doc: Arch §3 (FR-1, FR-2, FR-3, FR-4)]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import F, Q

from urbenmend.identity.models import Role, User
from urbenmend.identity.services import require_role

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbenmend.classification.models import Category
    from urbenmend.identity.models import User


def category_scope_for(user: User) -> QuerySet[Category]:
    """The categories an Authority is scoped to, newest-first by canonical name (BR-26, T1.5).

    Serializes to API §6.2's `"categoryScope": ["roads","water_drainage"]` — hence the ordering,
    which comes from `Category.Meta.ordering`: an unordered M2M read returns rows in whatever
    order Postgres happens to produce, so the same account's `GET /users/me` could differ between
    two calls for no reason a client could explain.

    ⚠️ **This is a display read, not the authorization check.** The check is
    `services.has_category_scope()`, which asks the database whether one category is present.
    Rendering the list here and comparing against it in a caller would reintroduce the
    fetch-everything-into-Python pattern that function avoids, and would put an authorization
    decision outside the service layer (FR-3, R-12).

    ⚠️ **Returns only what the relation holds — an Admin gets an empty queryset, not everything.**
    Admins bypass scope rather than being scoped (see `services.has_category_scope`), so an empty
    list here means "unrestricted" for an Admin and "can act on nothing" for an Authority. The
    role is what disambiguates; do not infer capability from this list alone.
    """
    return user.category_scope.all()


def list_users(*, actor: User, role: str | None = None, status: str | None = None, query: str = ""):
    require_role(actor, Role.ADMIN)
    queryset = User.objects.prefetch_related("category_scope").annotate(created_at=F("date_joined"))
    if role:
        queryset = queryset.filter(role=role)
    if status:
        queryset = queryset.filter(status=status)
    if query:
        queryset = queryset.filter(Q(email__icontains=query) | Q(phone__icontains=query))
    return queryset.order_by("-date_joined", "-pk")
