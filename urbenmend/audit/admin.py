"""
Audit & Integrity — Django admin registrations.

Reference data and moderation tooling are surfaced through admin [doc: Arch §2.4, FR-30/31].

[doc: Arch §3 (FR-32)]
"""

from django.contrib import admin

from urbenmend.audit.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "target_content_type", "target_object_id")
    list_filter = ("action", "target_content_type")
    search_fields = ("action", "target_object_id", "actor__email")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)
