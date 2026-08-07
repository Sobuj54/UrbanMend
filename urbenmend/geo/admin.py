"""
Geospatial — Django admin registrations.

Reference data and moderation tooling are surfaced through admin [doc: Arch §2.4, FR-30/31].
The `CityBoundary` editor is the operator-facing half of "the boundary is data, not code"
(NFR-11, BR-34): swapping a served-city polygon must not require a deploy.

[doc: Arch §3 (FR-6, FR-16, FR-17, FR-23, NFR-1); data-model §16; BR-35, C-11]
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.geo.models import CityBoundary

# django-stubs types ModelAdmin as generic in the model, but the runtime class is not
# subscriptable — see the same alias in `identity/admin.py` for the full reasoning.
if TYPE_CHECKING:
    _CityBoundaryAdminBase = admin.ModelAdmin[CityBoundary]
else:
    _CityBoundaryAdminBase = admin.ModelAdmin


@admin.register(CityBoundary)
class CityBoundaryAdmin(_CityBoundaryAdminBase):
    """Inspect and retire served-city boundaries (BR-34, NFR-11).

    ⚠️ **Unlike `CategoryAdmin`, adding is permitted.** The taxonomy is reviewed data whose slugs
    other tables key on, so it is migration-only; a boundary is a single opaque polygon nothing
    references by value, and `docs/city-boundary/README.md` documents replacement as an admin
    operation. Barring add here would make the documented swap impossible without a deploy —
    exactly what NFR-11 rules out.

    ⚠️ **`area` is read-only once saved, so replacement is add-and-retire.** Editing a live
    polygon in place rewrites the record of why past reports were accepted or rejected (`202`
    vs. `422 OUT_OF_CITY`); a new row plus an `is_active` flip keeps both decisions explainable.
    `is_active` stays editable — that flip *is* the retirement.

    ⚠️ **Deleting is barred** (database.md "No hard deletes"), for the same reason.
    """

    list_display = ["name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name"]

    def has_delete_permission(self, request: HttpRequest, obj: CityBoundary | None = None) -> bool:
        """No delete — retire with `is_active = False` instead (database.md)."""
        return False

    def get_readonly_fields(
        self, request: HttpRequest, obj: CityBoundary | None = None
    ) -> list[str]:
        """`area` is immutable once saved; a new boundary is a new row.

        Editable on the add form so a replacement polygon can be entered at all, frozen
        afterwards so an existing boundary's geometry cannot be rewritten under reports that
        were already validated against it.
        """
        if obj is None:
            return ["id", "created_at"]
        return ["id", "area", "created_at"]
