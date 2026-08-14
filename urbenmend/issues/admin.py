"""Django admin for clustering rules and read-only Issue/Confirmation inspection."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from urbenmend.issues.models import ClusteringRule, Confirmation, Issue

if TYPE_CHECKING:
    _IssueAdminBase = admin.ModelAdmin[Issue]
    _ClusteringRuleAdminBase = admin.ModelAdmin[ClusteringRule]
    _ConfirmationAdminBase = admin.ModelAdmin[Confirmation]
else:
    _IssueAdminBase = admin.ModelAdmin
    _ClusteringRuleAdminBase = admin.ModelAdmin
    _ConfirmationAdminBase = admin.ModelAdmin


@admin.register(ClusteringRule)
class ClusteringRuleAdmin(_ClusteringRuleAdminBase):
    """Admin-managed clustering configuration affecting future reports only (T4.3)."""

    list_display = [
        "category",
        "radius_m",
        "time_window_hours",
        "status",
        "updated_at",
    ]
    list_editable = ["radius_m", "time_window_hours", "status"]
    list_filter = ["status", "category"]
    search_fields = ["category__slug", "category__name_en", "category__name_bn"]
    autocomplete_fields = ["category"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["category__name_en", "-created_at"]

    def has_delete_permission(
        self, request: HttpRequest, obj: ClusteringRule | None = None
    ) -> bool:
        """Retire rules so past clustering configuration remains inspectable."""
        return False


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
        "corroborating_reporter_count",
        "opened_at",
    ]
    list_filter = ["status", "computed_severity", "overridden_severity", "primary_category"]
    search_fields = ["id", "computed_severity_rationale", "severity_override_reason"]
    raw_id_fields = [
        "primary_category",
        "severity_overridden_by",
        "assignee",
        "duplicate_of",
        "reopened_from",
    ]
    date_hierarchy = "opened_at"

    @admin.display(description="Severity")
    def display_severity(self, obj: Issue) -> str:
        return obj.current_severity

    @admin.display(description="Reports")
    def member_report_count(self, obj: Issue) -> int:
        return obj.report_count

    @admin.display(description="Corroboration")
    def corroborating_reporter_count(self, obj: Issue) -> int:
        return obj.corroboration_count

    def get_readonly_fields(self, request: HttpRequest, obj: Issue | None = None) -> list[str]:
        """Issue mutations must go through later audited domain services, never admin forms."""
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Issues arise only from clustering; there is deliberately no manual creation path."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Issue | None = None) -> bool:
        """Issues are moderated or merged and retained as history, never hard-deleted."""
        return False


@admin.register(Confirmation)
class ConfirmationAdmin(_ConfirmationAdminBase):
    """Inspect confirmations without bypassing the citizen-owned revocation endpoint."""

    list_display = ["id", "issue", "citizen", "created_at"]
    search_fields = ["id", "issue__id", "citizen__email", "citizen__phone"]
    raw_id_fields = ["issue", "citizen"]
    readonly_fields = ["id", "issue", "citizen", "created_at"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Confirmation | None = None) -> bool:
        return False
