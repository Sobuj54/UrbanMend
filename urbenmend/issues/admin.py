"""Read-only Django admin inspection for Issues (T4.1)."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.issues.models import Issue

if TYPE_CHECKING:
    _IssueAdminBase = admin.ModelAdmin[Issue]
else:
    _IssueAdminBase = admin.ModelAdmin


@admin.register(Issue)
class IssueAdmin(_IssueAdminBase):
    """Inspect authority work without bypassing the audited service-layer workflows."""

    list_display = [
        "id",
        "primary_category",
        "display_severity",
        "status",
        "assignee",
        "member_report_count",
        "opened_at",
    ]
    list_filter = ["status", "computed_severity", "overridden_severity", "primary_category"]
    search_fields = ["id", "computed_severity_rationale", "severity_override_reason"]
    raw_id_fields = [
        "primary_category",
        "severity_overridden_by",
        "assignee",
        "duplicate_of",
    ]
    date_hierarchy = "opened_at"

    @admin.display(description="Severity")
    def display_severity(self, obj: Issue) -> str:
        return obj.current_severity

    @admin.display(description="Reports")
    def member_report_count(self, obj: Issue) -> int:
        return obj.report_count

    def get_readonly_fields(self, request: HttpRequest, obj: Issue | None = None) -> list[str]:
        """Issue mutations must go through later audited domain services, never admin forms."""
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Issues arise only from clustering; there is deliberately no manual creation path."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Issue | None = None) -> bool:
        """Issues are moderated or merged and retained as history, never hard-deleted."""
        return False
