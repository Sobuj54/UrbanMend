"""
Reporting — Django admin registrations.

Reference data and moderation tooling are surfaced through admin [doc: Arch §2.4, FR-30/31].

[doc: Arch §3 (FR-5, FR-8, FR-9, FR-11)]
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.reporting.models import Report

# django-stubs types ModelAdmin as generic in the model, but the runtime class is not
# subscriptable — see the same alias in `identity/admin.py` for the full reasoning.
if TYPE_CHECKING:
    _ReportAdminBase = admin.ModelAdmin[Report]
else:
    _ReportAdminBase = admin.ModelAdmin


@admin.register(Report)
class ReportAdmin(_ReportAdminBase):
    """Read-only inspection of citizen submissions.

    ⚠️ **Fully read-only, and add is barred.** A Report is citizen-authored evidence: every
    field here either came from the submitter (`description`, `location`, `language`) or from
    async triage (the classification block, FR-10). An admin edit to either would be an
    unattributed rewrite of someone else's submission — FR-11's correction path is the API, which
    records `classification_source = authority`, and moderation is FR-31's hide/remove, not a
    form edit. `has_add_permission` is false for the same reason: a hand-made Report would have
    an author who never submitted it.

    ⚠️ **`status` is not editable here either.** It is the Report's *processing* lifecycle, driven
    by the classification worker (T3.5) and clustering (T4.5); setting it by hand would strand a
    row as `triaged` with no Issue, or re-queue one that is already classified.

    ⚠️ **No delete** (database.md "No hard deletes") — a removed Report takes an Issue's
    corroboration count (FR-16) with it. Moderation sets `status = removed` and the read returns
    `410` (API §4.2).
    """

    list_display = [
        "id",
        "author",
        "category",
        "status",
        "severity_signal",
        "classification_source",
        "created_at",
    ]
    list_filter = ["status", "severity_signal", "classification_source", "language", "category"]
    search_fields = ["id", "description", "address"]
    # `author` and `category` are FKs to tables that grow without bound; the raw-id widget avoids
    # a select box that loads every user on every page render.
    raw_id_fields = ["author", "category"]
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request: HttpRequest, obj: Report | None = None) -> list[str]:
        """Every concrete field, derived rather than listed.

        A hand-written list silently stops covering a column the day one is added — and the
        columns still to come here are the classification result (T3.5) and the Issue FK (T4.5),
        exactly the ones an admin must not hand-edit.
        """
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """No add — a Report is a citizen submission, not an admin artefact."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Report | None = None) -> bool:
        """No delete — moderation hides or removes by status (FR-31, database.md)."""
        return False
