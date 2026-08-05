"""
T1.2 — registration and channel verification (FR-1, API §6.1).

Layered per `testing.md`: service-level tests own the business rules, view-level tests own the
contract (status codes, envelope, camelCase). The service tests are the ones that would catch a
rule quietly moving into a view.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from urbenmend.identity import services
from urbenmend.identity.models import Channel, Role, User, UserStatus, VerificationCode

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------------------
# register_citizen — service layer
# ---------------------------------------------------------------------------------------


def test_register_with_email_creates_unverified_citizen() -> None:
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")

    assert user.role == Role.CITIZEN
    assert user.status == UserStatus.REGISTERED
    # Verification is timestamps, not booleans (A6). Unverified is NULL, both channels.
    assert user.email_verified_at is None
    assert user.phone_verified_at is None


def test_register_hashes_the_password() -> None:
    """⚠️ A stored plaintext password is the defect this test exists to prevent."""
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")

    assert user.password != "correct-horse-1"
    assert user.check_password("correct-horse-1")
    # PASSWORD_HASHERS puts Argon2 first [doc: auth.md, settings/base.py].
    assert user.password.startswith("argon2")


def test_register_normalizes_email_case() -> None:
    """`Foo@x.test` and `foo@x.test` are one mailbox, so they must not become two accounts."""
    services.register_citizen(email="Citizen@Example.TEST", password="correct-horse-1")

    assert User.objects.get().email == "citizen@example.test"


def test_register_with_phone_only_is_valid() -> None:
    user = services.register_citizen(phone="+8801712345678", password="correct-horse-1")

    assert user.phone == "+8801712345678"
    assert user.email is None


def test_register_without_any_contact_is_rejected() -> None:
    with pytest.raises(services.RegistrationError):
        services.register_citizen(password="correct-horse-1")


def test_register_duplicate_email_raises_conflict() -> None:
    services.register_citizen(email="citizen@example.test", password="correct-horse-1")

    with pytest.raises(services.RegistrationError):
        services.register_citizen(email="citizen@example.test", password="different-pass-2")

    assert User.objects.count() == 1


def test_register_duplicate_is_detected_after_normalization() -> None:
    """The UNIQUE index sees the normalized value, so a case variant must still conflict."""
    services.register_citizen(email="citizen@example.test", password="correct-horse-1")

    with pytest.raises(services.RegistrationError):
        services.register_citizen(email="CITIZEN@example.test", password="different-pass-2")


# ---------------------------------------------------------------------------------------
# send_verification_code
# ---------------------------------------------------------------------------------------


def test_send_verification_code_stores_a_hash_not_the_code() -> None:
    """⚠️ The code is a credential. A DB read must not yield a working code."""
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")

    verification, code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    assert verification.code_hash.startswith("argon2")
    # The plaintext must not be recoverable from the row.
    assert code not in verification.code_hash
    assert verification.consumed_at is None
    assert verification.attempts == 0
    assert not verification.is_expired


def test_send_verification_code_rejects_a_channel_the_user_lacks() -> None:
    from django.core.exceptions import ValidationError

    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")

    with pytest.raises(ValidationError):
        services.send_verification_code(user=user, channel=Channel.PHONE)


# ---------------------------------------------------------------------------------------
# verify_code
# ---------------------------------------------------------------------------------------


def test_verify_code_marks_channel_verified_on_success() -> None:
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    _verification, code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    result = services.verify_code(user=user, channel=Channel.EMAIL, code=code)

    assert result is True
    user.refresh_from_db()
    assert user.email_verified_at is not None
    assert user.phone_verified_at is None


def test_verify_code_consumes_the_code() -> None:
    """A code works once. Replaying it is a 422 (API §6.1)."""
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    verification, code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    services.verify_code(user=user, channel=Channel.EMAIL, code=code)
    verification.refresh_from_db()
    assert verification.consumed_at is not None

    with pytest.raises(services.VerificationError, match="already been used"):
        services.verify_code(user=user, channel=Channel.EMAIL, code=code)


def test_verify_code_rejects_wrong_code() -> None:
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    services.send_verification_code(user=user, channel=Channel.EMAIL)

    with pytest.raises(services.VerificationError, match="Invalid"):
        services.verify_code(user=user, channel=Channel.EMAIL, code="999999")


def test_verify_code_increments_attempts_on_failure() -> None:
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    verification, _code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    with pytest.raises(services.VerificationError):
        services.verify_code(user=user, channel=Channel.EMAIL, code="999999")

    verification.refresh_from_db()
    assert verification.attempts == 1


def test_verify_code_exhausts_after_max_attempts() -> None:
    """⚠️ The brute-force defence. A 6-digit code is 10^6 guesses; MAX_ATTEMPTS cuts it short."""
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    verification, _code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    for _ in range(VerificationCode.MAX_ATTEMPTS):
        with pytest.raises(services.VerificationError):
            services.verify_code(user=user, channel=Channel.EMAIL, code="999999")

    verification.refresh_from_db()
    assert verification.attempts == VerificationCode.MAX_ATTEMPTS
    assert not verification.is_usable

    # Now even the correct code is rejected.
    with pytest.raises(services.VerificationError, match="Too many failed attempts"):
        services.verify_code(user=user, channel=Channel.EMAIL, code="999999")


def test_verify_code_rejects_expired_code() -> None:
    from django.utils import timezone

    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    verification, code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    # Force expiry by backdating expires_at.
    verification.expires_at = timezone.now() - VerificationCode.TTL
    verification.save()

    with pytest.raises(services.VerificationError, match="expired"):
        services.verify_code(user=user, channel=Channel.EMAIL, code=code)


# ---------------------------------------------------------------------------------------
# POST /auth/register — view layer (contract)
# ---------------------------------------------------------------------------------------


def test_register_endpoint_returns_201_with_correct_shape(client) -> None:  # type: ignore[no-untyped-def]
    """API §6.1: `{userId, verificationRequired, channels}`."""
    response = client.post(
        reverse("api:auth-register"),
        data={"email": "citizen@example.test", "password": "correct-horse-1"},
        content_type="application/json",
    )

    assert response.status_code == 201
    body = response.json()
    # camelCase, not snake_case — the single easiest drift point (API §1.2, T0.6).
    assert "userId" in body
    assert "verificationRequired" in body
    assert body["channels"] == ["email"]


def test_register_endpoint_rejects_no_contact_with_400(client) -> None:  # type: ignore[no-untyped-def]
    response = client.post(
        reverse("api:auth-register"),
        data={"password": "correct-horse-1"},
        content_type="application/json",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"


def test_register_endpoint_conflict_returns_409(client) -> None:  # type: ignore[no-untyped-def]
    """API §6.1: duplicate identity → 409 CONFLICT."""
    client.post(
        reverse("api:auth-register"),
        data={"email": "citizen@example.test", "password": "correct-horse-1"},
        content_type="application/json",
    )

    response = client.post(
        reverse("api:auth-register"),
        data={"email": "citizen@example.test", "password": "different-pass-2"},
        content_type="application/json",
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CONFLICT"


# ---------------------------------------------------------------------------------------
# POST /auth/verify — view layer (contract)
# ---------------------------------------------------------------------------------------


def test_verify_endpoint_returns_200_on_success(client) -> None:  # type: ignore[no-untyped-def]
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    _verification, code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    response = client.post(
        reverse("api:auth-verify"),
        data={"channel": "email", "code": code, "identifier": "citizen@example.test"},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True


def test_verify_endpoint_returns_422_on_wrong_code(client) -> None:  # type: ignore[no-untyped-def]
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    services.send_verification_code(user=user, channel=Channel.EMAIL)

    response = client.post(
        reverse("api:auth-verify"),
        data={"channel": "email", "code": "999999", "identifier": "citizen@example.test"},
        content_type="application/json",
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"


def test_verify_endpoint_does_not_leak_account_existence(client) -> None:  # type: ignore[no-untyped-def]
    """⚠️ The endpoint is unauthenticated, so a distinguishable reply is an enumeration oracle.

    An unknown address and a known address with a wrong code must be indistinguishable —
    same status, same code, same message [doc: api-conventions.md, auth.md].
    """
    user = services.register_citizen(email="citizen@example.test", password="correct-horse-1")
    services.send_verification_code(user=user, channel=Channel.EMAIL)

    known = client.post(
        reverse("api:auth-verify"),
        data={"channel": "email", "code": "999999", "identifier": "citizen@example.test"},
        content_type="application/json",
    )
    unknown = client.post(
        reverse("api:auth-verify"),
        data={"channel": "email", "code": "999999", "identifier": "stranger@example.test"},
        content_type="application/json",
    )

    assert known.status_code == unknown.status_code == 422
    assert known.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert known.json()["error"]["code"] == unknown.json()["error"]["code"]


def test_verify_endpoint_accepts_the_identifier_as_typed(client) -> None:  # type: ignore[no-untyped-def]
    """Registration lowercases the stored email, so the lookup must normalize too.

    Otherwise someone who registered as `Citizen@Example.test` could never verify by typing
    it back the way they wrote it.
    """
    user = services.register_citizen(email="Citizen@Example.TEST", password="correct-horse-1")
    _verification, code = services.send_verification_code(user=user, channel=Channel.EMAIL)

    response = client.post(
        reverse("api:auth-verify"),
        data={"channel": "email", "code": code, "identifier": "Citizen@Example.TEST"},
        content_type="application/json",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.email_verified_at is not None
