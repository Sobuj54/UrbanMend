"""
Identity & Access — persistence.

The custom user model. **Declared before the first migration and irreversible afterwards**
[doc: Plan T0.10/T1.1, Arch §2.4] — changing `AUTH_USER_MODEL` later means dropping the
database and starting over.

Authorization model [doc: Arch §2.4, Plan T1.5, BR-26]: an explicit `role` field plus an
authority↔category scope relation, evaluated in `services.py`. `django.contrib.auth`
Groups/Permissions are deliberately **not** the RBAC mechanism — they cannot express the
per-category scoping BR-26 requires.

⚠️ `PermissionsMixin` is nonetheless inherited, because Django admin needs `is_staff`,
`is_superuser` and `has_perm()` to function at all, and FR-30/31 surface reference data and
moderation through admin. That is admin plumbing only. Domain authorization — who may see or
act on which Issue — is decided by `role` + category scope in the service layer, never by a
Group. Do not put domain RBAC in `groups` or `user_permissions`.

[doc: Arch §3 (FR-1, FR-2, FR-3, FR-4); domain entity in docs/03-data-model.md §1]
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    """Capability tier [doc: PRD §4.2, data-model §1].

    Values are the lowercase strings the API emits (`"role":"authority"`, API §6.2) — the
    wire format and the stored value are deliberately identical so no mapping layer can
    drift between them.
    """

    CITIZEN = "citizen", _("Citizen")
    AUTHORITY = "authority", _("Authority")
    ADMIN = "admin", _("Admin")


class UserStatus(models.TextChoices):
    """Account lifecycle [doc: data-model §"Entity Lifecycle" → User].

    registered → verified → active → suspended → deprovisioned, with deleted reachable as
    the anonymization branch (P6, BR-33, C-14). Transition rules are service-layer concerns
    (T1.1/T1.9); this enum only fixes the vocabulary.
    """

    REGISTERED = "registered", _("Registered (unverified)")
    VERIFIED = "verified", _("Verified")
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")
    DEPROVISIONED = "deprovisioned", _("Deprovisioned")
    DELETED = "deleted", _("Deleted (PII anonymized)")


class Language(models.TextChoices):
    """Preferred UI/notification language (NFR-8, API §6.2 `preferredLanguage`).

    Mirrors `settings.LANGUAGES` but is declared locally on purpose: referencing the setting
    would bake it into the migration and make every settings tweak a schema change.
    """

    ENGLISH = "en", _("English")
    BANGLA = "bn", _("Bangla")


# Storage format is E.164 so an SMS provider can consume it without further parsing. The
# API layer may accept a local Bangladeshi format and normalize on the way in (T1.2);
# ⚠️ Q5 (notification channels) is unresolved, so no provider is assumed here.
phone_validator = RegexValidator(
    regex=r"^\+[1-9]\d{7,14}$",
    message=_("Enter the phone number in E.164 format, for example +8801712345678."),
)


class UserManager(BaseUserManager["User"]):
    """Creation helpers.

    `use_in_migrations` is off: the manager is not needed by any migration, and enabling it
    would freeze this class's import path into the migration graph.
    """

    use_in_migrations = False

    def _create(
        self,
        *,
        email: str | None,
        phone: str | None,
        password: str | None,
        **extra: Any,
    ) -> User:
        if not email and not phone:
            raise ValueError("A user requires an email address or a phone number (data-model §1).")
        user = self.model(email=email, phone=phone, **extra)
        if password:
            user.set_password(password)
        else:
            # OTP-only accounts are legitimate — verification is by phone/email code
            # (FR-1). An unusable password cannot be used to authenticate.
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(
        self,
        email: str | None = None,
        phone: str | None = None,
        password: str | None = None,
        **extra: Any,
    ) -> User:
        extra.setdefault("role", Role.CITIZEN)
        extra.setdefault("status", UserStatus.REGISTERED)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email=email, phone=phone, password=password, **extra)

    def create_superuser(
        self,
        email: str | None = None,
        password: str | None = None,
        **extra: Any,
    ) -> User:
        if not email:
            raise ValueError("A superuser requires an email address.")
        if not password:
            raise ValueError("A superuser requires a password.")
        extra["role"] = Role.ADMIN
        extra["status"] = UserStatus.ACTIVE
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create(email=email, phone=extra.pop("phone", None), password=password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Any person interacting with UrbanMend in an authenticated capacity.

    One entity carries the role rather than one table per role, because a single account may
    change tier over time — an Admin grant promotes a Citizen to Authority (BR-25) and must
    not orphan the reports they already authored [doc: data-model §1].
    """

    # Opaque, non-sequential PK. API §1.2 requires IDs in URLs to be unguessable; a
    # sequential integer would leak user counts and enable enumeration.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # "email and/or phone" (data-model §1): each is individually optional, but the
    # has_contact_or_anonymized constraint below requires at least one until the account is
    # anonymized. NULL rather than "" for absence — Postgres allows many NULLs under a
    # UNIQUE index but only one "".
    email = models.EmailField(_("email address"), unique=True, null=True, blank=True)
    phone = models.CharField(
        _("phone number"),
        max_length=16,  # E.164: '+' plus at most 15 digits.
        unique=True,
        null=True,
        blank=True,
        validators=[phone_validator],
    )

    # Timestamps, not booleans: the API exposes `verified: {email, phone}` (API §6.2), which
    # is derivable from these, while the reverse loses when verification happened — needed
    # for the T2 trust signal and for audit (FR-32).
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CITIZEN)
    status = models.CharField(
        max_length=16, choices=UserStatus.choices, default=UserStatus.REGISTERED
    )
    preferred_language = models.CharField(
        max_length=8, choices=Language.choices, default=Language.ENGLISH
    )

    # Authority↔Category scope (BR-26, T1.5). "Authority **many ↔ many** Category (scope)"
    # [doc: data-model §1 Relationships]; DM-A3 scopes against Category deliberately, with a
    # Department entity deferred.
    #
    # ⚠️ **An empty scope grants nothing, and that is the whole design.** `has_category_scope()`
    # in services.py asks whether the category is present, so a newly provisioned Authority with
    # no rows can act on nothing until an Admin scopes them (BR-25). The alternative reading —
    # "empty means unrestricted" — turns a forgotten provisioning step into full access to every
    # category, which is exactly the failure BR-26 exists to prevent.
    #
    # ⚠️ **Set on any role, meaningful only on Authority.** Nothing at the database level stops
    # scope rows on a Citizen, because a role can change over time (BR-25 promotes a Citizen) and
    # a constraint would have to be dropped to allow it. Enforcement is service-layer: the scope
    # is consulted only after `role == AUTHORITY`, so stray rows on a Citizen grant nothing.
    # Admins bypass scope entirely rather than being scoped to everything — see
    # `require_category_scope()`.
    category_scope = models.ManyToManyField(
        "classification.Category",
        related_name="scoped_authorities",
        blank=True,
        help_text=_(
            "Categories an Authority may view and act on (BR-26). Ignored for other roles."
        ),
    )

    # ⚠️ The T1/T2 reporter trust signal is likewise absent by design. T2 defines it as
    # "newer/unverified accounts weigh less", which is computable from date_joined plus the
    # verification timestamps above. Storing a score would invent a weighting the docs do
    # not specify — and FR-21 already removed the one tunable numeric score from the design.

    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Whether this user may sign in to the Django admin site (FR-30/31)."),
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Enforces "email and/or phone" at the database level, with an escape hatch for
            # anonymization: DELETE /users/me clears PII but retains the row so public Issue
            # records keep referential integrity (P6, BR-33, C-14). Without the DELETED
            # branch the constraint would make the required anonymization impossible.
            models.CheckConstraint(
                condition=(
                    models.Q(email__isnull=False)
                    | models.Q(phone__isnull=False)
                    | models.Q(status=UserStatus.DELETED)
                ),
                name="identity_user_has_contact_or_anonymized",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # GET /users?role=&status= is the admin account list (API §6.2).
            models.Index(fields=["role", "status"], name="identity_user_role_status_idx"),
        ]

    def __str__(self) -> str:
        return self.email or self.phone or str(self.pk)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        """Whether the account may authenticate.

        Derived from `status` rather than stored, so there is one source of truth — a real
        `is_active` column could contradict `status`. `registered` counts as active: an
        unverified account can sign in but has limited capability and cannot be notified on
        an unverified channel (BR-30).

        ⚠️ Read-only, so Django's `ModelBackend.user_can_authenticate` honours it but admin
        cannot filter or bulk-edit on it. Revoking access also requires deleting the
        session rows — `is_active` alone does not end a live session (Arch §8, T1.3).

        The `override` ignore is deliberate: `AbstractBaseUser.is_active` is a writable
        `bool` and this narrows it to read-only, which mypy correctly flags as unsound.
        Narrowing is the point — `user.is_active = False` now raises AttributeError instead
        of setting a value that `status` would silently contradict. Callers must set
        `status` (T1.9).
        """
        return self.status in {
            UserStatus.REGISTERED,
            UserStatus.VERIFIED,
            UserStatus.ACTIVE,
        }

    def _normalize_contact(self) -> None:
        """Collapse blank contact fields to NULL and casefold the email.

        Both matter for correctness, not tidiness: two accounts stored as `""` would collide
        on the UNIQUE index while two NULLs do not, and `Foo@x.com` vs `foo@x.com` would
        otherwise become two accounts for one mailbox.
        """
        self.email = self.email.strip().lower() if self.email else None
        self.phone = self.phone.strip() if self.phone else None

    def clean(self) -> None:
        # Not calling super(): AbstractBaseUser.clean() normalizes USERNAME_FIELD
        # unconditionally and raises TypeError when it is None, which is a legitimate state
        # here for phone-only accounts.
        self._normalize_contact()

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Normalizing in save() as well as clean(): DRF serializers do not call full_clean(),
        # so clean() alone would leave the API path unnormalized.
        self._normalize_contact()
        super().save(*args, **kwargs)


class Channel(models.TextChoices):
    """A contact channel that can be verified [doc: API §6.1 `{"channel": "phone"}`].

    Values match the wire format exactly, for the same reason `Role` does: a mapping layer
    between stored and emitted values is a place for them to drift apart.
    """

    EMAIL = "email", _("Email")
    PHONE = "phone", _("Phone")


class VerificationCode(models.Model):
    """A single-use code proving control of one contact channel (FR-1, T1.2).

    ⚠️ **The code is stored hashed, never in plaintext.** It is a credential: it is the only
    thing standing between a stranger who knows an address and a verified account on it. A
    DB read — a backup, a log of a query, an admin inspecting rows — must not yield working
    codes, which is exactly the reasoning applied to passwords. The hasher is the project's
    configured Argon2 [doc: auth.md], so both credentials share one policy.

    ⚠️ **Rows are retained after use, not deleted.** `consumed_at` marks a code spent. A
    delete-on-use design cannot distinguish "this code never existed" from "this code was
    already used", and the second is a replay attempt worth being able to see.

    Why a table and not Redis: Redis here is also the cache and rate-limit store, so a code
    is subject to eviction under memory pressure — which would strand a user mid-registration
    with no way to tell them why. The attempt counter also has to increment atomically with
    the check it guards (`select_for_update`), which a table gives directly.
    """

    # Length and lifetime are policy, not derived from any spec statement — API §6.1 fixes
    # only the request/response shape and that an expired code yields 422. Six digits is the
    # OTP convention users expect from an SMS; the brute-force defence is MAX_ATTEMPTS below,
    # not the keyspace.
    CODE_LENGTH = 6
    TTL = timedelta(minutes=10)

    # ⚠️ The real protection on a 6-digit code. 10**6 guesses is trivially brute-forceable, so
    # the code is retired after this many failures and a fresh one must be requested. Returns
    # 429 per API §6.1 ("too many attempts"). This is per-code and intrinsic to the lifecycle;
    # per-IP/per-account request throttling is T1.8's separate concern.
    MAX_ATTEMPTS = 5

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "identity.User",
        on_delete=models.CASCADE,
        related_name="verification_codes",
    )
    channel = models.CharField(max_length=8, choices=Channel.choices)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("verification code")
        verbose_name_plural = _("verification codes")
        indexes: ClassVar[list[models.Index]] = [
            # The verify path's only query: newest live code for (user, channel).
            models.Index(
                fields=["user", "channel", "-created_at"],
                name="identity_vcode_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.channel} code for {self.user_id}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self) -> bool:
        """Not yet spent, not expired, and attempts remain."""
        return (
            self.consumed_at is None and not self.is_expired and self.attempts < self.MAX_ATTEMPTS
        )
