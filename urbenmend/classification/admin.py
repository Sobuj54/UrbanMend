"""
Classification — Django admin.

[doc: Arch §3, FR-30 "admin-editable config"]
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.classification.models import Category, SeverityKeyword

# django-stubs types ModelAdmin as generic in the model, but the runtime class is not
# subscriptable — see the same alias in `identity/admin.py` for the full reasoning.
if TYPE_CHECKING:
    _CategoryAdminBase = admin.ModelAdmin[Category]
    _SeverityKeywordAdminBase = admin.ModelAdmin[SeverityKeyword]
else:
    _CategoryAdminBase = admin.ModelAdmin
    _SeverityKeywordAdminBase = admin.ModelAdmin


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


@admin.register(SeverityKeyword)
class SeverityKeywordAdmin(_SeverityKeywordAdminBase):
    """Admin-managed severity keywords (FR-30, NFR-11, data-model §14).

    ⚠️ **Editable, unlike `CategoryAdmin` above — and the contrast is the design, not an
    inconsistency.** A category change ripples into authority scope (BR-26), clustering and every
    historical classification, so it belongs in a reviewed migration. A keyword change ripples
    nowhere: it changes what the *next* classification matches. FR-30 asks for exactly that tuning
    loop, and the operator who needs it most is the one watching fallback accuracy during an LLM
    outage — when shipping a migration is the last thing they can do.

    ⚠️ **The form is the only path that runs `MinLengthValidator`.** Validators fire in
    `full_clean()`, which `ModelForm` calls and `bulk_create` does not — so a one-character term is
    rejected here while the seeded migration stays unblocked. `SeverityKeyword.save()` normalizes the
    term on the way in, which is why the list shows the *matched* form rather than what was typed.
    """

    list_display = ["term", "severity", "category", "language", "status", "updated_at"]
    # ⚠️ `severity` and `status` are the two an operator changes in an incident, so both are
    # editable from the changelist — retiring a rule that is mis-firing should not cost two
    # page loads per rule.
    list_editable = ["severity", "status"]
    list_filter = ["status", "severity", "language", "category"]
    search_fields = ["term"]
    autocomplete_fields = ["category"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["term"]

    def has_delete_permission(
        self, request: HttpRequest, obj: SeverityKeyword | None = None
    ) -> bool:
        """No delete — `Active → Retired`, like every other reference entity (data-model §14).

        ⚠️ Retiring rather than deleting is what keeps FR-15's rationales legible: a report explains
        its severity by quoting the phrase that matched, and an operator reading that explanation
        months later needs the rule to still exist to understand it. `database.md` names Severity
        Keywords in the no-hard-delete list for this reason.
        """
        return False
