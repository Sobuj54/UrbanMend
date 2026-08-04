"""
Identity & Access — Django admin registrations.

Reference data and moderation tooling are surfaced through admin [doc: Arch §2.4, FR-30/31].

[doc: Arch §3 (FR-1, FR-2, FR-3, FR-4)]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import User

# django-stubs types ModelAdmin/UserAdmin as generic in the model, but the runtime classes are
# not subscriptable — `BaseUserAdmin[User]` raises TypeError when Django autodiscovers this
# module. Aliasing under TYPE_CHECKING gives mypy --strict the type argument it requires while
# the runtime base stays plain. `from __future__ import annotations` does not help here: the
# subscript is in a base-class list, which is evaluated eagerly.
if TYPE_CHECKING:
    _UserAdminBase = BaseUserAdmin[User]
else:
    _UserAdminBase = BaseUserAdmin


@admin.register(User)
class UserAdmin(_UserAdminBase):
    """Admin registration for the custom User model.

    Inherits BaseUserAdmin for the default password-change, admin-log, and permission-toggle
    wiring. That base class expects the model to carry `date_joined` and `is_staff`, which we
    supply — it does not require `username` or `is_active` as columns.
    """

    # ⚠️ Neither `is_active` nor `username` appears — `is_active` is a derived property and
    # admin cannot filter on it; username does not exist on this model (data-model §1).

    ordering = ["-date_joined"]
    search_fields = ["email", "phone"]
    list_display = [
        "email",
        "phone",
        "role",
        "status",
        "is_staff",
        "date_joined",
    ]
    list_filter = ["role", "status", "is_staff"]

    fieldsets = [
        (
            _("Identity"),
            {"fields": ["email", "phone"]},
        ),
        (
            _("Verification"),
            {"fields": ["email_verified_at", "phone_verified_at"]},
        ),
        (
            _("Role & Status"),
            {"fields": ["role", "status", "preferred_language"]},
        ),
        (
            _("Admin — permissions"),
            {
                "fields": [
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ],
            },
        ),
        (
            _("Dates"),
            {"fields": ["date_joined", "last_login"]},
        ),
    ]
    readonly_fields = ["date_joined", "last_login"]

    # BaseUserAdmin.add_fieldsets names `username`, which does not exist on this model — the
    # add form would raise admin.E012 unloaded. Email + password only; role/status default
    # via the model, and phone-only accounts are created through the API (T1.2), not admin.
    add_fieldsets = [
        (
            None,
            {
                "classes": ["wide"],
                "fields": ["email", "password1", "password2"],
            },
        ),
    ]

    # The base class REQUIRED_FIELDS default is empty, which is correct — USERNAME_FIELD is
    # email, and that is already in fieldsets. Adding email here would make the admin add-form
    # require it twice.
