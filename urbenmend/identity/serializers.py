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

from urbenmend.api.serializers import CamelCaseModelSerializer, CamelCaseSerializer
from urbenmend.identity.models import Channel, User


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

    Spec body: `{ "user": { "id", "role", "preferredLanguage" }, "requires2fa": false }`.

    ⚠️ Exactly those three user fields. Not `email`/`phone`/`status` — a login response is
    the easiest place to over-serialize, and contact details are precisely what API §2.1
    says the API never hands out.
    """

    user = serializers.SerializerMethodField()
    # ⚠️ Declared as `requires2fa`, NOT `requires_2fa`. The camelCase mixin renames on the
    # `_x` boundary, so `requires_2fa` would emit `requires2fa` — the same string — but only
    # by coincidence of the digit following the underscore. Spelling it as the spec does
    # removes the coincidence from the contract.
    requires2fa = serializers.SerializerMethodField()

    def get_user(self, obj: User) -> dict[str, str]:
        return {
            "id": str(obj.id),
            "role": obj.role,
            "preferredLanguage": obj.preferred_language,
        }

    def get_requires2fa(self, obj: User) -> bool:
        """Always `False` until T1.7 wires `django-otp` (FR-4).

        ⚠️ The field is in the contract now, so it ships now — a client written against
        the spec must not have to handle its absence. `False` is the truthful value while
        no user can have a confirmed OTP device: there is nothing to require.

        ⚠️ When T1.7 lands, this becomes a real check AND `LoginView` must stop issuing a
        full session in the same breath — the spec puts `/auth/2fa/verify` on a *partial*
        post-password session. Returning `True` here without that change would tell the
        client 2FA is pending while already having granted full access.
        """
        return False


class UserSerializer(CamelCaseModelSerializer):
    """User resource shape for API responses (API §6.2)."""

    verified = serializers.SerializerMethodField()

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
            "date_joined",
        ]
        read_only_fields = ["id", "role", "status", "date_joined"]

    def get_verified(self, obj: User) -> dict[str, bool]:
        """API §6.2 `verified: {email, phone}` is derived from the _verified_at timestamps."""
        return {
            "email": obj.email_verified_at is not None,
            "phone": obj.phone_verified_at is not None,
        }
