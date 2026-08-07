"""
Classification — Django admin.

[doc: Arch §3, FR-30 "admin-editable config"]
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.classification.models import Category

# django-stubs types ModelAdmin as generic in the model, but the runtime class is not
# subscriptable — see the same alias in `identity/admin.py` for the full reasoning.
if TYPE_CHECKING:
    _CategoryAdminBase = admin.ModelAdmin[Category]
else:
    _CategoryAdminBase = admin.ModelAdmin


@admin.register(Category)
class CategoryAdmin(_CategoryAdminBase):
    """Admin-editable category taxonomy (FR-30, NFR-11).

    ⚠️ **Read-only after seeding.** The taxonomy is data, not code, but adding/retiring nodes
    is a deliberate data migration, not a form edit — every category change affects authority
    scoping (BR-26), clustering rules, and historical classification. Edit via migration;
    the admin is for inspection only.
    """

    list_display = ["slug", "name_en", "name_bn", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["slug", "name_en", "name_bn"]
    # ⚠️ `slug` is read-only for a stronger reason than the labels are: authority-scope rows,
    # clustering rules and client filters all key on it, and an admin edit here would silently
    # orphan every one of them.
    readonly_fields = ["slug", "name_en", "name_bn", "status", "created_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """No add button — new categories come from migrations."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Category | None = None) -> bool:
        """No delete — lifecycle is Active → Retired, not deleted (data-model §5)."""
        return False
