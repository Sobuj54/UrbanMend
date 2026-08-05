"""
Identity & Access — write operations.

Every state change and every authorization check for this module lives here. This file
exists from day one even while empty: R-12 is the risk that "service-layer discipline
erodes under Django's idiom, scattering authorization into views/serializers", and the
named mitigation is that the convention is already in place, so putting a rule in a view
is never the path of least resistance.

Rules for this file [doc: Arch §3.1, FR-3]:
  - Callers pass the acting user; functions authorize before mutating. DRF permission
    classes are defence-in-depth, never the enforcement point.
  - Wrap multi-write operations in `transaction.atomic`.
  - Enqueue Celery tasks via `transaction.on_commit` so a worker cannot observe an
    uncommitted row [doc: Arch §2.4, §4.1].
  - Reads belong in selectors.py.

[doc: Arch §3 (FR-1, FR-2, FR-3, FR-4)]
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from urbenmend.identity.models import Channel, User, VerificationCode


class RegistrationError(Exception):
    """Registration-specific errors (conflict, validation)."""

    pass


class VerificationError(Exception):
    """Verification-specific errors (invalid code, expired, too many attempts)."""

    pass


@transaction.atomic
def register_citizen(
    *,
    email: str | None = None,
    phone: str | None = None,
    password: str,
    preferred_language: str = "en",
) -> User:
    """Register a new citizen account (FR-1, T1.2).

    Args:
        email: Email address (optional if phone provided).
        phone: Phone number in E.164 format (optional if email provided).
        password: Plaintext password (hashed with Argon2 before storage).
        preferred_language: UI/notification language (default: English).

    Returns:
        The created User instance with status=REGISTERED.

    Raises:
        RegistrationError: If neither email nor phone provided, or identity already exists.
        ValidationError: If email/phone format invalid.

    [doc: API §6.1 POST /auth/register, auth.md]
    """
    from urbenmend.identity.models import Role, User, UserStatus

    if not email and not phone:
        raise RegistrationError("At least one of email or phone is required.")

    try:
        user = User.objects.create_user(
            email=email,
            phone=phone,
            password=password,
            preferred_language=preferred_language,
            role=Role.CITIZEN,
            status=UserStatus.REGISTERED,
        )
    except IntegrityError as exc:
        # UNIQUE constraint on email/phone — the identity is already registered.
        # API §6.1: return 409 CONFLICT with a generic message (no user enumeration).
        raise RegistrationError("This email or phone number is already registered.") from exc
    except ValidationError:
        # Email/phone format validation from the model's clean()/validators.
        raise

    return user


@transaction.atomic
def send_verification_code(*, user: User, channel: Channel) -> tuple[VerificationCode, str]:
    """Issue a verification code for one contact channel (FR-1, T1.2).

    Args:
        user: The account to verify.
        channel: Which contact method to verify.

    Returns:
        `(verification, code)` — the row, and the **plaintext** code.

        ⚠️ The plaintext is returned rather than stored because it has to reach the user
        somehow and the database deliberately holds only the hash. It exists in memory for
        the duration of the send and nowhere else.

        ⚠️ **Never log it, never put it in a response body, never write it to the audit
        trail.** The only legitimate destination is the delivery adapter. It is returned as
        the second element rather than an attribute so that passing the row around — to a
        serializer, to a log line — cannot carry the secret with it by accident.

    Raises:
        ValidationError: If the account has no such channel to verify.

    [doc: API §6.1, auth.md]
    """
    from urbenmend.identity.models import Channel as ChannelEnum
    from urbenmend.identity.models import VerificationCode

    if channel == ChannelEnum.EMAIL and not user.email:
        raise ValidationError("This account has no email address to verify.")
    if channel == ChannelEnum.PHONE and not user.phone:
        raise ValidationError("This account has no phone number to verify.")

    # `secrets`, not `random` — this is a credential, and `random` is a predictable Mersenne
    # Twister an attacker who has seen prior codes could extrapolate from.
    code = "".join(secrets.choice("0123456789") for _ in range(VerificationCode.CODE_LENGTH))

    verification = VerificationCode.objects.create(
        user=user,
        channel=channel,
        # The project's configured Argon2 hasher, the same policy as passwords [doc: auth.md].
        code_hash=make_password(code),
        expires_at=timezone.now() + VerificationCode.TTL,
    )

    # ⚠️ Delivery is NOT wired up, deliberately. Which channels exist and which provider
    # sends them is ❓Q5, which is unresolved — CLAUDE.md forbids inventing an answer. When
    # Q5 lands, the send is enqueued here via `transaction.on_commit` so the worker cannot
    # observe an uncommitted row [doc: Arch §4.1]:
    #
    #     transaction.on_commit(lambda: deliver_verification_code.delay(verification.id, code))
    #
    # Until then the code is issued and verifiable, but nothing transmits it.

    return verification, code


def verify_code(*, user: User, channel: Channel, code: str) -> bool:
    """Verify a user's email or phone via the code they received (FR-1, T1.2).

    Marks the channel as verified on success. Invalid/expired codes increment the attempt
    counter; after MAX_ATTEMPTS the code is retired and a fresh one must be requested.

    Args:
        user: The user attempting verification.
        channel: Which contact method is being verified.
        code: The code the user received.

    Returns:
        True. Failure is always an exception, never `False` — a bool return invites
        `if verify_code(...)` written without an else, which fails open.

    Raises:
        VerificationError: If no code exists, the code is invalid/expired/spent, or the
            attempt limit is reached.

    ⚠️ **This function is deliberately NOT decorated `@transaction.atomic`, and must not
    be.** The attempt counter has to survive the exception that a wrong code raises. Under a
    single enclosing transaction the raise rolls the increment back with everything else, the
    counter stays at 0 forever, and MAX_ATTEMPTS never fires — an attacker gets unlimited
    guesses at a 6-digit code. The two atomic blocks below are separate so the increment is
    durable *before* the comparison that may reject it.

    [doc: API §6.1 POST /auth/verify, auth.md]
    """
    from urbenmend.identity.models import Channel as ChannelEnum
    from urbenmend.identity.models import VerificationCode

    # --- Transaction 1: claim an attempt. Commits before any rejection below. -----------
    with transaction.atomic():
        # `select_for_update` locks the row for the duration, so two concurrent attempts
        # cannot both read attempts=4 and both proceed past the MAX_ATTEMPTS check.
        verification = (
            VerificationCode.objects.filter(user=user, channel=channel)
            .select_for_update()
            .order_by("-created_at")
            .first()
        )

        # Each raise below happens before any write in this block, so the rollback discards
        # nothing — unlike the increment, these cost the caller no attempt.
        if verification is None:
            raise VerificationError("No verification code found for this channel.")

        if not verification.is_usable:
            if verification.consumed_at is not None:
                raise VerificationError("This code has already been used.")
            if verification.is_expired:
                raise VerificationError("This code has expired. Request a new one.")
            raise VerificationError("Too many failed attempts. Request a new code.")

        verification.attempts += 1
        verification.save(update_fields=["attempts"])

    # --- The attempt is now durable. A rejection past this point still counts. ----------
    # `check_password` is constant-time, so a wrong code leaks nothing through timing.
    if not check_password(code, verification.code_hash):
        raise VerificationError("Invalid verification code.")

    # --- Transaction 2: spend the code and mark the channel verified. ------------------
    with transaction.atomic():
        verification.consumed_at = timezone.now()
        verification.save(update_fields=["consumed_at"])

        # ⚠️ Compared against the enum, not `channel.value` — `Channel` is a `TextChoices`
        # member and therefore already a `str`, so a caller passing the bare wire string
        # `"email"` (which the serializer does) has no `.value` and would raise here.
        now = timezone.now()
        if channel == ChannelEnum.EMAIL:
            user.email_verified_at = now
            user.save(update_fields=["email_verified_at"])
        else:
            user.phone_verified_at = now
            user.save(update_fields=["phone_verified_at"])

    return True
