"""
Identity & Access — request/response shapes (T1.2).

Serializers for the authentication and user management endpoints. The camelCase mixin is
applied to every serializer here so the API emits `camelCase` per API §1.2, not DRF's default
`snake_case` [doc: api/serializers.py, Plan T0.6].

[doc: API §6.1 /auth, §6.2 /users]
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from urbenmend.api.serializers import (
    CamelCaseModelSerializer,
    CamelCaseSerializer,
    reject_unknown_fields,
    to_camel_case,
)
from urbenmend.identity import services
from urbenmend.identity.models import Channel, Role, User, UserStatus


class PasswordForgotSerializer(CamelCaseSerializer):
    identifier = serializers.EmailField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class PasswordResetSerializer(CamelCaseSerializer):
    reset_token = serializers.CharField(min_length=32, max_length=256)
    new_password = serializers.CharField(min_length=8, max_length=128, write_only=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class RegisterSerializer(CamelCaseSerializer):
    """POST /auth/register request body (API §6.1).

    At least one of email/phone required. The serializer validates this at the field level;
    the service enforces it with a transaction and raises RegistrationError on conflict.
    """

    email = serializers.EmailField(required=False, allow_blank=False)
    phone = serializers.CharField(required=False, allow_blank=False, max_length=16)
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    preferred_language = serializers.ChoiceField(
        choices=["en", "bn"],
        default="en",
        required=False,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """At least one contact method required (API §6.1, data-model §1)."""
        if not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError(
                "At least one of email or phone is required.",
                code="REQUIRED",
            )
        return attrs


class RegisterResponseSerializer(CamelCaseSerializer):
    """POST /auth/register 201 response (API §6.1)."""

    user_id = serializers.UUIDField(source="id")
    verification_required = serializers.BooleanField()
    channels = serializers.ListField(child=serializers.CharField())


class VerifyRequestSerializer(CamelCaseSerializer):
    """POST /auth/verify request body (API §6.1).

    For unauthenticated verification (pre-session), `identifier` is required to look up the
    user. For authenticated verification (adding a second channel), `identifier` is optional.
    """

    channel = serializers.ChoiceField(choices=[c.value for c in Channel])
    code = serializers.CharField(min_length=6, max_length=6)
    identifier = serializers.CharField(required=False, allow_blank=False)


class VerifyResponseSerializer(CamelCaseSerializer):
    """POST /auth/verify 200 response (API §6.1)."""

    verified = serializers.BooleanField()


class LoginSerializer(CamelCaseSerializer):
    """POST /auth/login request body (API §6.1).

    ⚠️ Shape only — no `EmailField`, no length or complexity rules on `password`. Every
    check here would answer a question the caller has not earned an answer to: rejecting a
    malformed identifier before the credential check tells an attacker their guess was not
    even a valid address, and a minimum length on login leaks the password policy. The
    service returns one generic failure for all of it.
    """

    identifier = serializers.CharField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)


class LoginResponseSerializer(CamelCaseSerializer):
    """POST /auth/login 200 response (API §6.1).

    Spec body: `{ "user": { "id", "role", "preferredLanguage" }, "requires2fa": false }`, or
    `{ "requires2fa": true }` alone when a second factor is still outstanding.

    ⚠️ Exactly those three user fields. Not `email`/`phone`/`status` — a login response is
    the easiest place to over-serialize, and contact details are precisely what API §2.1
    says the API never hands out.

    ⚠️ **`user` is omitted entirely while `requires2fa` is true** (API §6.1, amended
    2026-08-07). The caller has proved the password but not the second factor, and the role is
    the fact worth withholding: it tells a password-only holder whether the account they are
    part-way into is an Authority or an Admin, which is exactly the account worth continuing to
    attack. Emitting `null` instead of omitting would leak the same shape distinction.
    """

    user = serializers.SerializerMethodField()
    # ⚠️ Declared as `requires2fa`, NOT `requires_2fa`. The camelCase mixin renames on the
    # `_x` boundary, so `requires_2fa` would emit `requires2fa` — the same string — but only
    # by coincidence of the digit following the underscore. Spelling it as the spec does
    # removes the coincidence from the contract.
    requires2fa = serializers.SerializerMethodField()

    def get_user(self, obj: User) -> dict[str, str] | None:
        if self.get_requires2fa(obj):
            return None
        return {
            "id": str(obj.id),
            "role": obj.role,
            "preferredLanguage": obj.preferred_language,
        }

    def get_requires2fa(self, obj: User) -> bool:
        """Whether a second factor is outstanding for this account (FR-4, T1.7).

        True when an Admin set `require_two_factor` (T1.6) or the user confirmed a TOTP
        device. `LoginView` reads the *same* service function to decide whether to issue a
        partial session, so the body and the cookie can never disagree.
        """
        return services.requires_two_factor(user=obj)

    def to_representation(self, instance: User) -> dict[str, Any]:
        """Drop `user` from the payload when it is `None`, rather than emitting `null`."""
        data = super().to_representation(instance)
        if data.get("user") is None:
            data.pop("user", None)
        return data


class ProvisionAuthoritySerializer(CamelCaseSerializer):
    """POST /users/authorities request body (API §6.2, FR-2, BR-25).

    Spec body: `{ "email":"...", "categoryScope":["roads"], "requireTwoFactor": true }`.

    ⚠️ **No `role` field, and no `password`.** The role is not a caller choice — the endpoint's
    entire purpose is to create an *Authority*, so accepting `role` would let an Admin provision
    another Admin through a URL that is not documented to do that. The password is absent because
    the spec's body has none; see `provision_authority` for why generating one is worse.

    ⚠️ **`status` is not accepted either.** The service pins `registered` so the work address must
    be verified before the account is live — a caller-supplied `active` would skip that.

    ⚠️ **Shape validation only; every rule is re-checked in the service.** The `categoryScope`
    values are validated against the taxonomy in `_resolve_category_scope`, not here — a
    `ChoiceField` built from a queryset at import time would freeze the seven current nodes into
    the process and start rejecting any category a later migration adds until the pod restarts.
    """

    email = serializers.EmailField(required=True, allow_blank=False)
    phone = serializers.CharField(required=False, allow_blank=False, max_length=16)
    # `allow_empty=True`: an Authority provisioned with no scope can act on nothing, which is a
    # valid parked state (see `has_category_scope`). `required=False` because the spec marks
    # nothing in this body mandatory; absent means the same as `[]`.
    category_scope = serializers.ListField(
        child=serializers.SlugField(max_length=50),
        required=False,
        allow_empty=True,
        default=list,
    )
    require_two_factor = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """At least one contact method (data-model §1) — the same rule as registration.

        Duplicated in `provision_authority` on purpose: this copy produces the field-level
        `VALIDATION_FAILED` detail the spec asks for, while the service copy holds when the
        function is called from a management command with no serializer in sight (FR-3).
        """
        if not attrs.get("email"):
            raise serializers.ValidationError(
                "An authority account requires an email address.",
                code="REQUIRED",
            )
        return attrs


class UserSerializer(CamelCaseModelSerializer):
    """User resource shape for API responses (API §6.2).

    ✅ **The T1.6 ❓ is resolved: API §6.2 was amended (2026-08-07, T1.9) before `GET /users/me`
    shipped**, per the spec-first rule. The amendment fixes one response shape for all three
    roles, with `categoryScope` always an array.

    ⚠️ **`categoryScope` is the stored BR-26 rows, which is NOT the effective permission for two
    of the three roles.** It reads `[]` for a Citizen (scope does not gate them) and `[]` for an
    Admin — who bypasses scope entirely, `scoped_category_ids()` returning `None` for "apply no
    filter". So an Admin's `[]` and an unscoped Authority's `[]` are byte-identical JSON meaning
    opposite things: unrestricted, versus permitted nothing until an Admin scopes them. `role`
    disambiguates; the spec now says so explicitly and warns clients not to derive capability
    from this field alone.

    It is emitted for every role rather than omitted or `null` so the shape stays stable — the
    alternative makes a client branch on `role` before it can parse the body.
    """

    verified = serializers.SerializerMethodField()
    category_scope = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone",
            "role",
            "status",
            "preferred_language",
            "verified",
            "category_scope",
            "date_joined",
        ]
        read_only_fields = ["id", "role", "status", "date_joined"]

    def get_verified(self, obj: User) -> dict[str, bool]:
        """API §6.2 `verified: {email, phone}` is derived from the _verified_at timestamps."""
        return {
            "email": obj.email_verified_at is not None,
            "phone": obj.phone_verified_at is not None,
        }

    def get_category_scope(self, obj: User) -> list[str]:
        """API §6.2 `categoryScope: ["roads","water_drainage"]` — slugs, not labels or ids.

        Goes through the T1.5 selector rather than `obj.category_scope.all()` so the ordering is
        the one `category_scope_for` documents; two responses differing in array order for no
        reason a client could explain is a contract defect, not a cosmetic one.
        """
        from urbenmend.identity.selectors import category_scope_for

        return [category.slug for category in category_scope_for(obj)]


class AdminUserListQuerySerializer(CamelCaseSerializer):
    role = serializers.ChoiceField(choices=Role.choices, required=False)
    status = serializers.ChoiceField(choices=UserStatus.choices, required=False)
    q = serializers.CharField(required=False, allow_blank=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self, extra_allowed=("limit", "cursor"))
        return attrs


class AdminUserUpdateSerializer(CamelCaseSerializer):
    role = serializers.ChoiceField(choices=Role.choices, required=False)
    status = serializers.ChoiceField(choices=UserStatus.choices, required=False)
    category_scope = serializers.ListField(
        child=serializers.SlugField(max_length=50), required=False, allow_empty=True
    )
    require_two_factor = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        if not attrs:
            raise serializers.ValidationError("Provide at least one field.")
        if "category_scope" in attrs and attrs.get("role") not in {None, Role.AUTHORITY}:
            raise serializers.ValidationError(
                {"category_scope": "Category scope applies only to Authority accounts."}
            )
        return attrs


class ProfileUpdateSerializer(CamelCaseSerializer):
    """PATCH /users/me request body (API §6.2, amended 2026-08-07).

    Spec body: `{ "phone": "…", "preferredLanguage": "bn" }` — and that list is exhaustive.

    ⚠️ **A plain `Serializer`, not a `ModelSerializer` over `User`.** A model serializer's field
    set is whatever the model carries, so `role`, `status`, `is_staff` and `require_two_factor`
    would all be one `fields` edit — or one careless `"__all__"` — away from being
    self-assignable. Privilege escalation should require adding a field here, not forgetting to
    exclude one. Same reasoning `TwoFactorEnrollResponseSerializer` records for not wrapping
    `TOTPDevice`.

    ⚠️ **`email` is absent deliberately, not by oversight.** Self-service email change is excluded
    from the endpoint (spec amended 2026-08-07): the email is where a password reset is sent, so
    changing it from a live session converts a borrowed session into permanent account takeover.

    ⚠️ **Unknown fields are rejected, not ignored.** DRF's default is to drop them silently, so
    `PATCH {"role":"admin"}` would return `200` with a body still showing `role: "citizen"` — and
    a caller cannot distinguish that from a successful update. api-conventions.md asks for
    rejection "where strictness matters"; an endpoint whose neighbours are privilege fields is
    exactly that case.
    """

    # ⚠️ `allow_blank=True` but `allow_null=False`: `""` clears the number (the service hands it
    # to `save()`, which normalizes it to the NULL the UNIQUE index needs), while `null` is
    # refused. Accepting both spellings for one intent means a client sends `null` and hits the
    # E.164 validator as a `500` instead of a `400`.
    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=False,
        max_length=16,
    )
    preferred_language = serializers.ChoiceField(choices=["en", "bn"], required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Reject an empty body, and any field this endpoint does not own.

        ⚠️ An empty `PATCH` answering `200` is indistinguishable from a real update, so a client
        with a misspelled field name would see success forever.
        """
        # ⚠️ Both spellings are accepted because `CamelCaseSerializerMixin.to_internal_value()`
        # rewrites the keys *before* this runs while `initial_data` keeps the client's original —
        # comparing against `self.fields` alone would flag the caller's own `preferredLanguage` as
        # unknown. Derived from the declared fields rather than hardcoded so adding a field here
        # cannot forget to allow its camelCase form.
        # ⚠️ `.keys()`, not iteration over `self.fields`: DRF's `BindingDict` yields field *names*
        # at runtime, but its stubs type `__iter__` as yielding `Field`, so `to_camel_case(name)`
        # would not type-check. Iterating the keys explicitly says what is meant either way.
        declared = {str(name) for name in self.fields.keys()}  # noqa: SIM118
        allowed = declared | {to_camel_case(name) for name in declared}
        submitted = set(self.initial_data) if isinstance(self.initial_data, dict) else set()

        if unknown := sorted(submitted - allowed):
            raise serializers.ValidationError(
                dict.fromkeys(unknown, "This field is not editable through this endpoint."),
                code="VALIDATION_FAILED",
            )
        if not attrs:
            raise serializers.ValidationError(
                "Provide at least one field to update.",
                code="REQUIRED",
            )
        return attrs


class TwoFactorEnrollResponseSerializer(CamelCaseSerializer):
    """POST /auth/2fa/enroll 201 response (API §6.1, amended 2026-08-07).

    Spec body: `{ "secret": "BASE32…", "otpauthUri": "otpauth://totp/…", "confirmed": false }`.

    ⚠️ **This is the one and only place the TOTP secret is ever serialized.** It is a
    credential — never log it, never add it to `UserSerializer`, never expose it through a read
    endpoint or an admin field. There is no recovery path by design: a lost unconfirmed secret
    is replaced by enrolling again.

    ⚠️ Not a `ModelSerializer` over `TOTPDevice`. A model serializer would emit whatever fields
    the third-party model happens to carry — `key`, `last_t`, `drift` — and a django-otp upgrade
    adding a field would silently widen this response. The three fields the spec names are
    declared explicitly.
    """

    secret = serializers.CharField(read_only=True)
    otpauth_uri = serializers.CharField(read_only=True)
    confirmed = serializers.BooleanField(read_only=True)


class TwoFactorVerifySerializer(CamelCaseSerializer):
    """POST /auth/2fa/verify request body (API §6.1).

    Spec body: `{ "code": "…" }`.

    ⚠️ Shape only — no `min_length`, no digits-only regex, no `IntegerField`. Same reasoning as
    `LoginSerializer`: a validation error that fires before the code is checked tells an
    unauthenticated caller what a valid code looks like, and `verify_totp()` rejects everything
    wrong with one indistinguishable message anyway. `IntegerField` would additionally break
    the leading zeros TOTP codes routinely carry.
    """

    code = serializers.CharField(trim_whitespace=True)


class TwoFactorVerifyResponseSerializer(CamelCaseSerializer):
    """POST /auth/2fa/verify 200 response (API §6.1, amended 2026-08-07).

    Spec body: `{ "user": { "id", "role", "preferredLanguage" }, "confirmed": true }`.

    The `user` object matches `LoginResponseSerializer`'s exactly — this is the response that
    completes a login, so a client should not have to parse two shapes for one outcome.
    """

    user = serializers.SerializerMethodField()
    confirmed = serializers.SerializerMethodField()

    def get_user(self, obj: User) -> dict[str, str]:
        return {
            "id": str(obj.id),
            "role": obj.role,
            "preferredLanguage": obj.preferred_language,
        }

    def get_confirmed(self, obj: User) -> bool:
        """Always `True` on a `200` — reaching this line means a device accepted the code."""
        return True
