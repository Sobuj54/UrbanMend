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
