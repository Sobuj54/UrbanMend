"""
Identity & Access — Django admin registrations.

Reference data and moderation tooling are surfaced through admin [doc: Arch §2.4, FR-30/31].

[doc: Arch §3 (FR-1, FR-2, FR-3, FR-4)]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from .models import User, VerificationCode

# django-stubs types ModelAdmin/UserAdmin as generic in the model, but the runtime classes are
# not subscriptable — `BaseUserAdmin[User]` raises TypeError when Django autodiscovers this
# module. Aliasing under TYPE_CHECKING gives mypy --strict the type argument it requires while
# the runtime base stays plain. `from __future__ import annotations` does not help here: the
# subscript is in a base-class list, which is evaluated eagerly.
if TYPE_CHECKING:
    _UserAdminBase = BaseUserAdmin[User]
    _VerificationCodeAdminBase = admin.ModelAdmin[VerificationCode]
else:
    _UserAdminBase = BaseUserAdmin
    _VerificationCodeAdminBase = admin.ModelAdmin


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


@admin.register(VerificationCode)
class VerificationCodeAdmin(_VerificationCodeAdminBase):
    """Read-only view of issued verification codes (T1.2).

    ⚠️ **Fully read-only, and `code_hash` is never listed or exposed as a field.** Admin
    exists here for support ("did their code arrive, was it used, did they burn the
    attempts?") and for spotting abuse — none of which needs the credential itself.

    ⚠️ **Nothing here may be editable.** A writable `attempts` would let anyone with admin
    reset the brute-force counter, and a writable `consumed_at` would un-spend a used code.
    Both defeat the controls `services.verify_code()` enforces. Verification is a domain
    action; if support must help a stuck user, the fix is to issue a fresh code through the
    service, not to hand-edit this row.
    """

    list_display = ["user", "channel", "created_at", "expires_at", "consumed_at", "attempts"]
    list_filter = ["channel", "created_at"]
    search_fields = ["user__email", "user__phone"]
    ordering = ["-created_at"]

    # Every field, so the change form renders as a read-only inspection view.
    # ⚠️ `code_hash` is deliberately absent — there is no operational question it answers, and
    # displaying it invites offline cracking. One sequence feeds both `fields` and
    # `readonly_fields` so a field can never be added to the form but left writable.
    # ⚠️ A tuple, not a list. The two attributes are declared with different types upstream,
    # and a `list[str]` satisfies only `fields`: lists are invariant, so `list[str]` is not a
    # `list[str | list[str] | ...]`. Tuples are covariant, so one `tuple[str, ...]` fits both.
    _INSPECTION_FIELDS: ClassVar[tuple[str, ...]] = (
        "id",
        "user",
        "channel",
        "expires_at",
        "consumed_at",
        "attempts",
        "created_at",
    )
    readonly_fields = _INSPECTION_FIELDS
    fields = _INSPECTION_FIELDS

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Codes are issued by `services.send_verification_code()`, which is what generates the
        # secret and (once Q5 lands) delivers it. An admin-created row would have no
        # deliverable code behind it.
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: VerificationCode | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: VerificationCode | None = None
    ) -> bool:
        # Retention is deliberate: a consumed row is the evidence that a code was used, and
        # deleting it erases the difference between "never existed" and "already spent".
        return False
