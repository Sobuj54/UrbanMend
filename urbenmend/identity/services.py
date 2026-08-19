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
from importlib import import_module
from typing import TYPE_CHECKING, Any

import structlog
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.http import HttpRequest
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from urbenmend.classification.models import Category
    from urbenmend.identity.models import Channel, User, VerificationCode

# ⚠️ The audit trail for T1.6 rides this logger until T8.1 replaces it with the real table — see
# `_audit_privileged_action`. Not `logging.getLogger`: the project's processor chain is structlog's
# (T0.9), so a stdlib logger would emit an unstructured line into a JSON stream and lose the
# `trace_id` that `contextvars` binds per request.
logger = structlog.get_logger(__name__)


def request_password_reset(*, identifier: str) -> None:
    """Issue an email-only reset token while keeping the caller response generic."""
    from urbenmend.identity.models import PasswordResetToken, User

    user = User.objects.filter(email=identifier.strip().lower()).first()
    if user is None or not user.email or user.email_verified_at is None or not user.is_active:
        return
    raw_token = secrets.token_urlsafe(32)
    PasswordResetToken.objects.filter(user=user, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )
    PasswordResetToken.objects.create(
        user=user,
        token_hash=make_password(raw_token),
        expires_at=timezone.now() + PasswordResetToken.TTL,
    )
    transaction.on_commit(
        lambda: send_mail(
            subject="UrbanMend password reset",
            message=f"Your password reset token is: {raw_token}",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@urbanmend.local"),
            recipient_list=[user.email],
        )
    )


@transaction.atomic
def reset_password(*, reset_token: str, new_password: str) -> None:
    """Consume one valid reset token, change the password and revoke every session."""
    from django.contrib.auth.password_validation import validate_password
    from django.contrib.sessions.models import Session

    from urbenmend.identity.models import PasswordResetToken

    candidates = (
        PasswordResetToken.objects.select_for_update()
        .select_related("user")
        .filter(
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
            attempts__lt=PasswordResetToken.MAX_ATTEMPTS,
        )
        .order_by("-created_at")
    )
    matched = None
    for candidate in candidates:
        if check_password(reset_token, candidate.token_hash):
            matched = candidate
            break
    if matched is None:
        raise ValidationError("The reset token is invalid or expired.")
    validate_password(new_password, user=matched.user)
    matched.user.set_password(new_password)
    matched.user.save(update_fields=["password"])
    matched.consumed_at = timezone.now()
    matched.save(update_fields=["consumed_at"])
    for session in Session.objects.all().iterator():
        data = session.get_decoded()
        if data.get(SESSION_KEY) == str(matched.user_id):
            session.delete()


class RegistrationError(Exception):
    """Registration-specific errors (conflict, validation)."""

    pass


class VerificationError(Exception):
    """Verification-specific errors (invalid code, expired, too many attempts)."""

    pass


class AuthenticationError(Exception):
    """Bad credentials — the generic login failure (API §6.1 `401`)."""

    pass


class AccountLockedError(AuthenticationError):
    """The credentials were correct but the account may not authenticate.

    ⚠️ A distinct class, not a message variant, because the two map to different status
    codes: bad credentials are `401 UNAUTHENTICATED`, a locked account is `403
    ACCOUNT_LOCKED` (API §6.1). A view that had to string-match the message to tell them
    apart would break the moment the wording changed.

    Subclasses `AuthenticationError` so `except AuthenticationError` still catches both —
    the fail-closed direction. An `except` clause that misses this would let a suspended
    account through, so the inheritance makes the safe reading the default one.
    """

    pass


# ⚠️ One message for every login failure — unknown identifier and wrong password alike.
# API §6.1: "401 (bad credentials — generic message, no user enumeration)". Splitting this
# into two strings is the whole vulnerability, so it is a module constant rather than two
# literals that could drift apart.
_GENERIC_LOGIN_FAILURE = "Invalid credentials."

# ⚠️ Likewise one message for every second-factor failure (T1.7) — wrong code, expired code,
# replayed code, and no-device-enrolled all read identically. A caller on a partial session has
# proved a password but not an identity, so "no authenticator enrolled" would disclose another
# account's security posture. API §6.1 gives all of them `422`.
_GENERIC_TWO_FACTOR_FAILURE = "The verification code is invalid or has expired."


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


def authenticate_user(*, identifier: str, password: str) -> User:
    """Authenticate an email/phone + password pair (FR-1, T1.3).

    Args:
        identifier: Email address or E.164 phone number, as the user typed it.
        password: The plaintext password to check.

    Returns:
        The authenticated user.

    Raises:
        AuthenticationError: The identifier is unknown or the password is wrong. One
            message for both — API §6.1 requires a generic reply so login cannot be used
            to enumerate accounts.
        AccountLockedError: The password was correct but `status` forbids authenticating
            (suspended, deprovisioned, deleted).

    ⚠️ **The password is checked even when the identifier is unknown.** Returning early on
    a missing user makes the unknown-account path measurably faster than the wrong-password
    path, and that timing difference is an enumeration oracle just as surely as a different
    message would be. Hashing a throwaway value keeps the two paths comparable.

    [doc: API §6.1 POST /auth/login, auth.md, api-conventions.md "no user enumeration"]
    """
    from urbenmend.identity.models import User

    # Normalized exactly as `User.save()` normalizes on the way in, or someone who
    # registered as `Citizen@Example.test` (stored lowercased) could never log in by typing
    # it back the way they wrote it.
    identifier = identifier.strip()
    user: User | None
    try:
        if "@" in identifier:
            user = User.objects.get(email=identifier.lower())
        else:
            user = User.objects.get(phone=identifier)
    except User.DoesNotExist:
        user = None

    if user is None:
        # ⚠️ Not a bare `raise`. Argon2 is deliberately slow, so skipping the hash here would
        # make "no such account" the fast path and leak existence through response time.
        # `set_password` runs the configured hasher and throws the result away — this is the
        # same mitigation `django.contrib.auth.backends.ModelBackend` applies, for the same
        # reason (Django #20760).
        User().set_password(password)
        raise AuthenticationError(_GENERIC_LOGIN_FAILURE)

    if not user.check_password(password):
        raise AuthenticationError(_GENERIC_LOGIN_FAILURE)

    # ⚠️ Checked *after* the password, deliberately. Reporting "locked" to someone who has
    # not proved they own the account would confirm it exists and reveal its moderation
    # state; reaching this line means they supplied the right password.
    #
    # `is_active` is derived from `status` (A6) — registered/verified/active authenticate,
    # suspended/deprovisioned/deleted do not. An unverified account CAN log in; BR-30 limits
    # what it may then do, which is not this function's concern.
    if not user.is_active:
        raise AccountLockedError("This account is not permitted to sign in.")

    return user


def start_session(*, request: HttpRequest, user: User) -> None:
    """Attach a server-validated session to `request` for `user` (T1.3).

    `django.contrib.auth.login()` writes the session row, cycles the session key, and sets
    `request.user`; Django's `SessionMiddleware` then emits the cookie on the way out with
    the `Secure`/`HttpOnly`/`SameSite` flags from settings (API §2, set in A4).

    ⚠️ **The key cycling is the security-relevant part, not a detail.** `login()` calls
    `cycle_key()`, which discards the pre-login anonymous session and issues a new
    identifier. Without it, a token an attacker planted in the victim's browser before login
    would still be valid after it — session fixation. Writing `request.session[...]` by hand
    instead of calling `login()` would silently drop that protection.

    ⚠️ **`backend=` is passed explicitly.** `login()` reads `user.backend` when the argument
    is omitted, and that attribute only exists if the user came from `django.contrib.auth
    .authenticate()`. Ours comes from `authenticate_user()` above — a service function, not
    an auth backend — so omitting it raises. The named backend is the one in
    `AUTHENTICATION_BACKENDS`; it is recorded in the session and used to reload the user on
    subsequent requests.

    [doc: API §2, Arch §8, auth.md]
    """
    django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")


def end_session(*, request: HttpRequest) -> None:
    """Revoke the caller's own session (T1.3, API §6.1 `POST /auth/logout`).

    `django.contrib.auth.logout()` flushes the session — deleting the `django_session` row
    *and* the cached copy — then clears `request.user`. The cookie is expired by
    `SessionMiddleware` on the response.

    ⚠️ **No authorization check, and none is needed.** The only session this can end is the
    one on the request, so a caller can revoke nothing but their own. That is why this is
    the one identity service without an actor argument. Revoking *another* user's sessions
    is `revoke_all_sessions()` below, which is a different operation with a different
    caller.

    Idempotent: calling it without a session is a no-op, so a double-submitted logout does
    not error.
    """
    django_logout(request)


def revoke_all_sessions(*, user: User) -> int:
    """Delete every active session belonging to `user`, server-side (T1.3).

    Returns:
        The number of sessions destroyed.

    This is the capability that justified choosing sessions over JWT in the first place
    (Arch §8): moderation suspends an account (BR-25) or a user is deprovisioned or
    anonymized (BR-33, C-14), and their live sessions must stop working *now*, not whenever
    a token happens to expire. A stateless token cannot do this without a revocation list,
    which is a session table wearing a disguise.

    ⚠️ **Deleting the `django_session` rows directly is NOT sufficient on the `cached_db`
    backend, and doing so is a silent security hole.** `cached_db.SessionStore.load()` reads
    Redis first and only falls back to the table on a miss, so a session whose row is gone
    but whose cache entry is still warm keeps authenticating until the cache expires — up to
    `SESSION_COOKIE_AGE` later. Going through `SessionStore(key).delete()` removes both
    copies. A `Session.objects.filter(...).delete()` would pass a naive test (the row really
    is gone) while leaving the session live.

    ⚠️ **Sessions are scanned, not queried by user.** `django_session` stores the user id
    inside the opaque encoded blob with no column or index for it, so there is nothing to
    filter on — every unexpired row has to be decoded. That is acceptable because this runs
    on suspension and deletion, not per request. If the table ever grows enough for this to
    hurt, the fix is a `user`-keyed index table written at login, not a query against this
    one.

    [doc: Arch §8, API §2 "session revocation is immediate", BR-25/BR-33]
    """
    from django.contrib.sessions.models import Session

    session_store = import_module(settings.SESSION_ENGINE).SessionStore
    target_id = str(user.pk)
    revoked = 0

    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        # `get_decoded()` returns {} on a tampered or undecodable payload rather than
        # raising, so a corrupt row cannot abort the revocation of the others.
        if session.get_decoded().get(SESSION_KEY) != target_id:
            continue
        session_store(session_key=session.session_key).delete()
        revoked += 1

    return revoked


# =======================================================================================
# Profile read/update (T1.9, API §6.2 `PATCH /users/me`)
# =======================================================================================


class ProfileUpdateError(Exception):
    """A profile update collided with another account's contact detail (T1.9).

    Maps to `409 CONFLICT` — API §6.2 lists `409` (identity in use) for `PATCH /users/me`.
    Distinct from `ProvisioningError` and `RegistrationError` despite the shared status code:
    the disclosure rules differ again. This caller is authenticated and updating *their own*
    profile, so telling them the number is taken reveals nothing they could not learn by trying
    to register it — and withholding it would leave them unable to tell a typo from a conflict.
    """


def update_profile(
    *,
    user: User,
    phone: str | None = None,
    preferred_language: str | None = None,
) -> User:
    """Update the caller's own contact channel and language (API §6.2 `PATCH /users/me`).

    Args:
        user: The account to update. **Authorization is structural** — this is "self", and the
            view passes `request.user`, so there is no id in the body or the path to tamper
            with. That is why there is no `require_role` here: every authenticated caller may
            edit their own profile, and no caller can name a different one (FR-3 is satisfied by
            the absence of a selector, not by an omitted check).
        phone: New E.164 number, or `""` to clear it. `None` means "not submitted" — distinct
            from `""`, which is an explicit removal. Re-triggers phone verification (BR-30).
        preferred_language: `"en"` or `"bn"` (NFR-8).

    Returns:
        The updated user, refreshed.

    ⚠️ **`email` is deliberately not updatable here.** Self-service email change is excluded from
    `PATCH /users/me` (API §6.2, amended 2026-08-07): it is the address a password reset is sent
    to, so allowing it turns one live session into a permanent account takeover. Changing an
    email is an Admin action (`PATCH /users/{id}`). This function's caller must never receive a
    change of email as a success.

    Raises:
        ProfileUpdateError: `409` — the new phone is already in use by a different account.
        ValidationError: `422` — clearing the phone would leave the account with no contact
            channel.
    """
    from urbenmend.identity.models import Channel, User

    normalized: str | None = None

    if phone is not None:
        # ⚠️ Normalized exactly as `User._normalize_contact()` does it, because the point of the
        # check is to predict what the UNIQUE index will see. The same `.strip().lower()` pattern
        # `provision_authority` records: `normalize_email` lowercases only the domain, so using it
        # here would let a collision through to an `IntegrityError` instead of the documented 409.
        normalized = phone.strip()

        if not normalized:
            if user.email is None:
                raise ValidationError(
                    "An account must keep at least one contact channel — the email address or a "
                    "phone number."
                )
        # ⚠️ `.exclude(pk=user.pk)`: the caller's own current number is not a conflict. Without
        # it, re-submitting an unchanged profile would `409` against itself.
        elif User.objects.filter(phone=normalized).exclude(pk=user.pk).exists():
            raise ProfileUpdateError("An account with that phone number already exists.")

    updated_fields: list[str] = []

    if phone is not None:
        updated_fields += ["phone", "phone_verified_at"]
        user.phone = normalized
        # ⚠️ **Cleared whenever the phone is submitted, even if the value is unchanged.** The
        # timestamp is a claim that *this* number was proven, and re-verification is cheap; the
        # alternative is comparing against the stored value and skipping when equal, which keeps
        # a stale claim alive for a number that changed hands and came back. Fail closed:
        # `verified.phone` reads `false` until a fresh code is confirmed (BR-30).
        user.phone_verified_at = None

    if preferred_language is not None:
        updated_fields.append("preferred_language")
        user.preferred_language = preferred_language

    try:
        with transaction.atomic():
            # One UPDATE for both fields — `save()` also re-runs `_normalize_contact()`, which is
            # what turns a submitted `""` into the NULL the UNIQUE index needs (models.py A6).
            user.save(update_fields=updated_fields)

            if normalized:
                # ⚠️ Inside the transaction, so a failure to issue the code rolls the number back
                # rather than leaving an unverifiable phone on the account. The Q5 delivery this
                # is waiting on enqueues via `transaction.on_commit`, so the worker still cannot
                # observe an uncommitted row [doc: Arch §4.1]. The plaintext code is discarded
                # here exactly as `RegisterView` discards it — it must never reach a response.
                send_verification_code(user=user, channel=Channel.PHONE)
    except IntegrityError as exc:
        # ⚠️ Not redundant with the `.exists()` above — it closes the window between that check
        # and this UPDATE, the same race `provision_authority` records for its INSERT. Two
        # accounts claiming one number concurrently would otherwise yield `500`, not the
        # documented `409`.
        raise ProfileUpdateError("An account with that phone number already exists.") from exc

    return user


# =======================================================================================
# Account deletion → PII anonymization (T1.9, P6, BR-33, C-14)
# =======================================================================================


class AccountDeletionError(Exception):
    """`DELETE /users/me` refused for a role that must not self-delete (API §6.2, amended 2026-08-07).

    Maps to `403 FORBIDDEN`. An Authority's lifecycle ends with an Admin's
    `PATCH /users/{id} {status:"deprovisioned"}`, not with the holder erasing an audited grant
    (FR-2, BR-25); an Admin's self-deletion risks removing the last account able to provision
    anyone. The 403 is deliberately not an `AuthorizationError`: this is a state of the *target*
    role, not of the actor's permission tier — and `AuthorizationError` subclasses Django's
    `PermissionDenied`, which the exception handler maps to the same 403 anyway.
    """


@transaction.atomic
def anonymize_account(*, user: User) -> None:
    """Permanently delete an account by anonymizing it (T1.9, P6, BR-33, C-14).

    ⚠️ **`DELETE /users/me` is Citizen-only** (API §6.2, amended 2026-08-07). The ownership
    matrix grants an Authority `RU` — not `D` — on its own account, and an Admin's self-deletion
    could strand the platform without a provisioner. The data-model "Ownership & Permissions"
    table, FR-2, and BR-25 all trace here; the spec amendment records the decision.

    ⚠️ **There is no undo and no second step.** The caller's own session is revoked *inside this
    transaction* (BR-33, Arch §8), the PII is nulled, and the response is written after commit —
    so the `202` is emitted to a caller that can no longer authenticate. The `202` status reflects
    that retained-record anonymization may extend past the response (P2/P3 add Reports and media),
    **not** that the account is still usable.

    ⚠️ **The row is retained, never deleted** (C-14): public Issue history keeps a stable author
    reference. `UserStatus.DELETED` is the CheckConstraint's escape hatch — `email`/`phone` are
    both NULL here, which the `identity_user_has_contact_or_anonymized` constraint would otherwise
    reject (models.py T1.9 comment). **Do not tighten that constraint.**

    ⚠️ **`is_active` is derived and read-only** (A6, models.py): `status=DELETED` is outside
    `{registered, verified, active}`, so the account cannot authenticate from the moment this
    transaction commits — the row stops being usable before the session rows are gone, not after.
    The session revocation is still required: a deleted `is_active` does not end a *live* session
    by itself (Arch §8, T1.3), and `cached_db` means a session row delete without the
    `SessionStore(key).delete()` path leaves the cached copy authenticating (revoke_all_sessions'
    entire reason for existing).
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from urbenmend.identity.models import Role, UserStatus, VerificationCode

    if user.role in (Role.ADMIN, Role.AUTHORITY):
        raise AccountDeletionError(
            "Authority and Admin accounts cannot be deleted through the self-service endpoint. "
            "Contact an administrator."
        )

    # ⚠️ **Order matters: status is flipped first.** Once `status=DELETED` commits, `is_active`
    # is False and no request can authenticate; the session revocation below is the last thing
    # that can use the session, and a re-entrant revocation cannot be interrupted mid-flight by a
    # credential that outlives the flip.
    user.status = UserStatus.DELETED
    # ⚠️ PII is nulled in the same statement as the status flip — never a separate UPDATE. A
    # crash between the two would leave a row that is `deleted` but still carries a live email
    # address, and the constraint's DELETED branch would happily accept it. Anonymization that
    # can silently fail halfway is not anonymization.
    user.email = None
    user.phone = None
    user.email_verified_at = None
    user.phone_verified_at = None
    user.save(update_fields=["status", "email", "phone", "email_verified_at", "phone_verified_at"])

    # ⚠️ The category-scope M2M is cleared so a deleted Authority-ish leftover cannot be scoped
    # retroactively, and the code/TOTP rows are removed because they are secrets and secrets are
    # not retained after the account that minted them is gone. `verificationcode` rows cascade on
    # user delete, but there is no delete here — the user row survives — so the cascade never
    # fires; these are explicit.
    user.category_scope.clear()
    VerificationCode.objects.filter(user=user).delete()
    TOTPDevice.objects.filter(user=user).delete()

    # ⚠️ Sessions are revoked last, and the revocation itself must never be skipped. A deleted
    # account's live session would keep authenticating until it expired — `is_active` alone does
    # not end a session (Arch §8) — which is the exact hole BR-33 exists to prevent.
    revoke_all_sessions(user=user)

    # Structured, T8.1-funnel style: not `_audit_privileged_action` (that one takes an actor and
    # an action; this is a self-service event whose audit semantics land with T8.1).
    logger.info("account_anonymized", user_id=str(user.pk), role=user.role)


# =======================================================================================
# RBAC — the enforcement layer (T1.5, FR-3, BR-26/BR-27)
# =======================================================================================
#
# ⚠️ **This is *the* enforcement point, not one of several.** FR-3 requires authorization on
# every server-side action and BR-27 on "every mutating and sensitive-read action". DRF
# permission classes are defence-in-depth [doc: auth.md, Arch §3.1] — a rule that lives only in
# a permission class is absent from every non-HTTP caller: a Celery task, a management command,
# another service. R-12 names the erosion of this discipline as a project risk, so the
# primitives live here and are imported by other apps' services rather than reimplemented.
#
# ⚠️ **`django.contrib.auth` Groups/Permissions are deliberately unused.** They cannot express
# BR-26's per-category scope: a permission is a global verb-on-model grant with no room for
# "roads and drainage but not electrical" [doc: auth.md, CLAUDE.md]. `PermissionsMixin` is
# inherited for Django-admin plumbing only.
#
# **Shape of these functions.** Each `require_*` raises and returns `None`; the `has_*`
# predicates return `bool` for the rare caller that must branch (building a queryset filter,
# rendering a capability list). Callers should reach for `require_*` by default — a predicate
# invites `if has_role(...)` written with no `else`, which fails open. The same reasoning
# `verify_code()` records for never returning `False`.


class ProvisioningError(Exception):
    """An Authority account could not be provisioned (T1.6).

    Maps to `409 CONFLICT` — API §6.2 lists `409` for `POST /users/authorities`. Distinct from
    `RegistrationError` because the two have opposite disclosure rules: registration is public and
    must not reveal whether an address is taken, while this endpoint is Admin-only and an admin
    who cannot be told "that address already has an account" cannot do the job.
    """


class AuthorizationError(PermissionDenied):
    """`403 FORBIDDEN` — the caller is authenticated but not permitted (API §4.2).

    ⚠️ Subclasses Django's `PermissionDenied` rather than DRF's, so `services.py` stays free of
    DRF imports [doc: Arch §3.1] and a service is callable from a Celery task without dragging
    the web framework in. `urbenmend_exception_handler` already translates Django's
    `PermissionDenied` to a `403 FORBIDDEN` envelope, so the API mapping is inherited with no
    view-level handling [doc: api/exceptions.py].

    ⚠️ **A `403` here is a deliberate choice per case, not a default.** Where replying at all
    would confirm that a resource exists, the caller must raise `Http404` instead — API §4.2:
    `404` covers "absent **or hidden from this caller**". §"Cross-cutting" fixes scope leakage at
    `403`/`404`, so both are correct; which one depends on whether existence is itself secret.
    The scoped-read helpers below make that choice explicit at the call site rather than
    silently.
    """


def has_role(user: User, *roles: str) -> bool:
    """Whether `user` currently holds any of `roles`.

    ⚠️ **`status` is checked too, not just `role`.** A suspended or deprovisioned Authority still
    has `role == "authority"` in the database; treating that as authorization would let a
    just-suspended account keep working until its session happened to expire, which is the exact
    failure sessions-over-JWT was chosen to avoid (Arch §8, T1.3). `is_active` is derived from
    `status` (A6).

    An unauthenticated caller reaches this as `AnonymousUser`, which has no `role`, so the
    `getattr` guard returns False rather than raising `AttributeError` — a crash here would
    surface as a `500` where the contract says `401` (API §4.2).
    """
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return user.role in roles


def require_role(user: User, *roles: str) -> None:
    """Assert `user` holds one of `roles`, else raise.

    Raises:
        AuthorizationError: The caller's role is not in `roles`, or the account is not active.

    ⚠️ The message names no role and no resource. "Authority role required" tells an attacker
    which capability tier to go after, and repeated across endpoints it maps the whole permission
    matrix from the outside.
    """
    if not has_role(user, *roles):
        raise AuthorizationError("You do not have permission to perform this action.")


def has_category_scope(user: User, category: Category) -> bool:
    """Whether `user` may act on `category` (BR-26).

    Resolution order, and each step matters:

    1. **Admin → always true.** PRD §4.2 gives Admin every Authority capability plus more. Admins
       bypass scope rather than being scoped to every category: seeding rows for each node would
       silently un-scope an admin the moment a new category is added by migration.
    2. **Not an active Authority → false.** A Citizen has no scope to consult, and a suspended
       Authority is not an Authority (see `has_role`).
    3. **Authority → membership test.** `.filter(pk=...).exists()` rather than
       `category in user.category_scope.all()`, which pulls every scoped row into Python on every
       check and, worse, caches the result on the instance — a scope revoked mid-request would
       keep passing.

    ⚠️ **An empty scope therefore grants nothing.** A freshly provisioned Authority is denied
    until an Admin scopes them. Reading empty as "unrestricted" would make a forgotten
    provisioning step into unrestricted access.
    """
    from urbenmend.identity.models import Role

    if has_role(user, Role.ADMIN):
        return True
    if not has_role(user, Role.AUTHORITY):
        return False
    return user.category_scope.filter(pk=category.pk).exists()


def require_category_scope(user: User, category: Category) -> None:
    """Assert `user` may act on `category`, else raise `403` (BR-26).

    Raises:
        AuthorizationError: `403 FORBIDDEN` — the caller is an Authority outside their scope, or
            lacks the role entirely.

    ⚠️ Use this when the caller already knows the resource exists — acting on an Issue they were
    shown, for instance. When a reply would itself disclose existence, use
    `require_scoped_visibility()` instead, which raises `404`.
    """
    if not has_category_scope(user, category):
        raise AuthorizationError("You do not have permission to act on this category.")


def require_scoped_visibility(user: User, category: Category) -> None:
    """Assert `user` may see a resource in `category`, else raise `404` (BR-26).

    Raises:
        Http404: The resource is outside the caller's scope.

    ⚠️ **`404`, not `403`, and the difference is the point.** API §4.2 defines `404` as "absent
    **or hidden from this caller**", and §"Cross-cutting" requires scope leakage to return
    `403`/`404` to "avoid existence leaks". A `403` on a scoped *read* is itself a disclosure: it
    confirms that an Issue with that id exists and merely belongs to another category, which lets
    an out-of-scope Authority enumerate ids and map the workload of departments they cannot see.
    A `404` is indistinguishable from a wrong id.

    ⚠️ **`Http404` is Django's, not DRF's `NotFound`** — same reason as `AuthorizationError`:
    no DRF import in the service layer. The exception handler translates it to the `404
    NOT_FOUND` envelope [doc: api/exceptions.py].
    """
    from django.http import Http404

    if not has_category_scope(user, category):
        raise Http404("No such resource.")


def scoped_category_ids(user: User) -> set[int] | None:
    """The category ids `user` may act on, or `None` meaning "no restriction".

    Returns:
        `None` for an Admin — unrestricted, and NOT the same as the set of all current category
        ids. A caller building a queryset filter must branch on `None` and apply no filter at
        all, so a category added after the request began is still included.

        A `set` for an Authority, empty when they are unscoped. An empty set filters everything
        out, which is the correct denial.

        An empty set for anyone else, so a mistaken call for a Citizen denies rather than grants.

    ⚠️ For list endpoints, this belongs in the queryset, not in a post-filter over fetched rows.
    Filtering after pagination would return short pages and let a client infer how many
    out-of-scope rows were dropped — and API §4.4 makes cursor pagination mandatory precisely
    because the authority queue is large and concurrently mutated.

    ⚠️ **Returns ids, not a queryset.** The scope is read once here rather than joined into
    every caller's query, so `selectors.py` in other apps needs no dependency on the shape of
    this relation.
    """
    from urbenmend.identity.models import Role

    if has_role(user, Role.ADMIN):
        return None
    if not has_role(user, Role.AUTHORITY):
        return set()
    return set(user.category_scope.values_list("pk", flat=True))


# =======================================================================================
# Authority provisioning (T1.6, FR-2, BR-25)
# =======================================================================================


def _audit_privileged_action(
    *,
    actor: User,
    action: str,
    target: User,
    before: dict[str, Any] | None = None,
    **detail: Any,
) -> None:
    """Persist and operationally log a privileged identity action (FR-32)."""
    from urbenmend.audit.services import record_event

    record_event(actor=actor, action=action, target=target, before=before, after=detail)
    logger.info(
        "privileged_action",
        action=action,
        actor_id=str(actor.pk),
        actor_role=actor.role,
        target_id=str(target.pk),
        **detail,
    )


def _resolve_category_scope(category_slugs: Sequence[str]) -> list[Category]:
    """Turn the spec's `["roads","water_drainage"]` into Category rows, or reject the request.

    Raises:
        ValidationError: `422` — a key is unknown, or names a Retired node.

    ⚠️ **Retired categories are rejected, not silently dropped.** A Retired node can never match an
    Issue, so scoping an authority to one grants them nothing while reading back as a successful
    grant — the provisioning bug hardest to notice, because the account looks correctly configured.
    `422` with the offending key is the honest answer.

    ⚠️ **Unknown keys are named in the message**, unlike the deliberately opaque RBAC denials. Both
    callers are Admin-only and the Admin supplied these keys, so there is no enumeration to
    protect: the taxonomy is public reference data (`GET /categories`, API §6.10).

    ⚠️ An empty sequence is valid and returns `[]` — an Authority scoped to nothing can act on
    nothing (see `has_category_scope`), which is a legitimate parked state, not an error.
    """
    from urbenmend.classification.models import Category, CategoryStatus

    # `dict.fromkeys` de-duplicates while keeping order, so `["roads","roads"]` is one grant
    # rather than a length mismatch reported as an unknown key.
    requested = list(dict.fromkeys(category_slugs))
    if not requested:
        return []

    categories = list(Category.objects.filter(slug__in=requested, status=CategoryStatus.ACTIVE))
    if len(categories) != len(requested):
        unknown = sorted(set(requested) - {category.slug for category in categories})
        raise ValidationError(f"Unknown or retired category: {', '.join(unknown)}.")
    return categories


@transaction.atomic
def provision_authority(
    *,
    actor: User,
    email: str | None = None,
    phone: str | None = None,
    category_slugs: Sequence[str],
    require_two_factor: bool = False,
) -> User:
    """Create an Authority account with its category scope (FR-2, BR-25, API §6.2).

    Args:
        actor: The Admin performing the grant. **Authorized here, in the service** (FR-3).
        email: Work address for the new account. At least one contact is required.
        phone: Alternative contact.
        category_slugs: The BR-26 scope. Machine keys from `Category.slug`, as the spec's
            `"categoryScope": ["roads"]` sends them.
        require_two_factor: Stored now, enforced by T1.7 (FR-4).

    Returns:
        The new Authority, scope already attached.

    Raises:
        AuthorizationError: `403` — `actor` is not an Admin. **BR-25 is this line.**
        ProvisioningError: `409` — the contact is already in use.
        ValidationError: `422` — no contact given, or a slug does not resolve to an active
            Category.

    ⚠️ **`require_role(actor, Role.ADMIN)` is the first statement, and BR-25 lives or dies on it.**
    "An Authority role can be granted only by an Admin" is not enforceable in a view: a Celery
    task or management command calling this function would bypass a permission class entirely.

    ⚠️ **The account gets no password** — `create_user(password=None)` sets an unusable one. The
    spec's body carries no password field, and inventing one would mean either an Admin choosing
    another person's credential or a generated secret travelling back through the API response.
    Neither is acceptable; the authority establishes their own via the reset flow (T1.7).
    ⚠️ Until T1.7 ships that flow, a provisioned authority **cannot yet log in**. The account,
    role and scope are all real; only the credential path is missing. Recorded, not hidden.

    ⚠️ **`status` stays `REGISTERED`, not `ACTIVE`.** The work address is unproven until someone
    reading that mailbox verifies it, and BR-30 bars notifications to an unverified channel. An
    Admin typo would otherwise create a live Authority whose owner never learns the account
    exists.

    ⚠️ **Scope is resolved and validated before the user is created**, so an unknown slug fails
    with nothing written. Inside `atomic` a later failure would roll back anyway, but ordering it
    this way keeps the error about the request rather than about a half-built account.
    """
    from urbenmend.identity.models import Role, User, UserStatus

    require_role(actor, Role.ADMIN)

    if not email and not phone:
        raise ValidationError("An authority account requires an email address or a phone number.")

    categories = _resolve_category_scope(category_slugs)

    # ⚠️ Normalized exactly as `User._normalize_contact()` does it, because the point of the check
    # is to predict what the UNIQUE index will see. `BaseUserManager.normalize_email` lowercases
    # only the domain, so `Admin@x.com` would pass this check and then be stored as `admin@x.com`
    # — the collision would surface as an IntegrityError instead of the 409 the spec asks for.
    normalized_email = email.strip().lower() if email else None
    normalized_phone = phone.strip() if phone else None

    if normalized_email and User.objects.filter(email=normalized_email).exists():
        raise ProvisioningError("An account with that email address already exists.")
    if normalized_phone and User.objects.filter(phone=normalized_phone).exists():
        raise ProvisioningError("An account with that phone number already exists.")

    try:
        authority = User.objects.create_user(
            email=normalized_email,
            phone=normalized_phone,
            password=None,
            role=Role.AUTHORITY,
            status=UserStatus.REGISTERED,
            require_two_factor=require_two_factor,
        )
    except IntegrityError as exc:
        # ⚠️ Not redundant with the `exists()` checks above — it closes the window between them
        # and this INSERT. Two Admins provisioning the same address concurrently both pass the
        # check; without this the loser gets a `500` instead of the documented `409`. The checks
        # remain because they distinguish *which* field collided, which the constraint error only
        # reports in a message no caller should be parsing.
        raise ProvisioningError(
            "An account with that email or phone number already exists."
        ) from exc

    if categories:
        authority.category_scope.set(categories)

    _audit_privileged_action(
        actor=actor,
        action="authority.provisioned",
        target=authority,
        category_scope=sorted(category.slug for category in categories),
        require_two_factor=require_two_factor,
    )
    return authority


@transaction.atomic
def set_category_scope(
    *,
    actor: User,
    authority: User,
    category_slugs: Sequence[str],
) -> User:
    """Replace an Authority's category scope (BR-25/BR-26, API §6.2 `PATCH /users/{id}`).

    Raises:
        AuthorizationError: `403` — `actor` is not an Admin.
        ValidationError: `422` — the target is not an Authority, or a slug is unknown/retired.

    ⚠️ **Replaces, does not merge.** The spec sends the whole array
    (`{"categoryScope":["roads","electrical"]}`), so a merge would make revocation impossible
    through the documented body — an Admin narrowing a scope would silently widen it instead.

    ⚠️ **An empty array is a valid, meaningful request**: it revokes all scope, leaving an
    Authority who can act on nothing. That is the intended way to park an account without
    suspending it, and it works because an empty scope grants nothing (see `has_category_scope`).
    """
    from urbenmend.identity.models import Role

    require_role(actor, Role.ADMIN)

    if authority.role != Role.AUTHORITY:
        # ⚠️ Checked against the column, not `has_role()`: scope must remain editable on a
        # suspended Authority, or an Admin could not correct a scope before reinstating them.
        raise ValidationError("Category scope applies only to Authority accounts (BR-26).")

    categories = _resolve_category_scope(category_slugs)
    previous_scope = sorted(authority.category_scope.values_list("slug", flat=True))
    authority.category_scope.set(categories)

    _audit_privileged_action(
        actor=actor,
        action="authority.scope_changed",
        target=authority,
        before={"category_scope": previous_scope},
        category_scope=sorted(category.slug for category in categories),
    )
    return authority


@transaction.atomic
def update_user_by_admin(*, actor: User, user_id, **changes: Any) -> User:
    """Apply the documented admin account controls and audit each resulting snapshot."""
    from urbenmend.identity.models import Role, User

    require_role(actor, Role.ADMIN)
    try:
        target = User.objects.select_for_update().get(pk=user_id)
    except (User.DoesNotExist, ValueError, TypeError) as exc:
        from django.http import Http404

        raise Http404("User not found.") from exc
    before = {
        "role": target.role,
        "status": target.status,
        "require_two_factor": target.require_two_factor,
        "category_scope": sorted(target.category_scope.values_list("slug", flat=True)),
    }
    resulting_role = changes.get("role", target.role)
    if "category_scope" in changes and resulting_role != Role.AUTHORITY:
        raise ValidationError("Category scope applies only to Authority accounts (BR-26).")
    if "category_scope" in changes:
        scope = _resolve_category_scope(changes.pop("category_scope"))
        target.category_scope.set(scope)
    for field in ("role", "status", "require_two_factor"):
        if field in changes:
            setattr(target, field, changes.pop(field))
    if target.role != Role.AUTHORITY and target.category_scope.exists():
        target.category_scope.clear()
    if changes:
        raise ValidationError("Unsupported user update field.")
    target.save(update_fields=["role", "status", "require_two_factor"])
    after = {
        "role": target.role,
        "status": target.status,
        "require_two_factor": target.require_two_factor,
        "category_scope": sorted(target.category_scope.values_list("slug", flat=True)),
    }
    from urbenmend.audit.services import record_event

    record_event(
        actor=actor, action="identity.user_updated", target=target, before=before, after=after
    )
    return target


# =======================================================================================
# Two-factor authentication — TOTP (T1.7, FR-4)
# =======================================================================================
#
# ⚠️ **The partial session deliberately does NOT call `django_login()`.** It writes one key into
# an anonymous session and nothing else, so `SESSION_KEY` (`_auth_user_id`) stays unset,
# `AuthenticationMiddleware` resolves `request.user` to `AnonymousUser`, and every endpoint in
# the project that requires authentication rejects the cookie with `401` — automatically, with
# no per-view opt-in. That is the whole design: a caller who has proved only the password holds
# a credential that unlocks `/auth/2fa/*` and nothing else.
#
# ⚠️ **django-otp's `otp_required` decorator model was considered and rejected for this.** It
# logs the user fully in first and then gates individual views, so a view added later without
# the decorator is reachable with one factor. It fails open; the missing-`_auth_user_id`
# approach fails closed. `OTPMiddleware` stays in the stack (it attaches `request.user.otp_device`
# and costs nothing), but it is not the enforcement point here.
#
# [doc: API §6.1 POST /auth/2fa/enroll + /auth/2fa/verify, auth.md, FR-4]

# The session key holding the id of a user who passed the password check but not yet the second
# factor. ⚠️ Must never be named `_auth_user_id` — that is `SESSION_KEY`, and setting it is
# exactly what makes a session fully authenticated.
PARTIAL_SESSION_USER_KEY = "urbenmend_partial_auth_user_id"

# ⚠️ Deliberately far shorter than `SESSION_COOKIE_AGE`. A half-finished login is a
# password-only credential sitting in a cookie; it should expire in the time it takes to read a
# code off a phone, not persist for the full session lifetime. Our policy, not spec-derived —
# API §6.1 fixes neither a TTL nor a code length.
PARTIAL_SESSION_TTL_SECONDS = 5 * 60


class TwoFactorError(Exception):
    """2FA-specific failures — no device enrolled, or the code was wrong/expired/replayed.

    ⚠️ One class for every failure mode, mapped to `422` by the view (API §6.1). The reason is
    the same one `VerifyView` records: distinguishing "no device enrolled" from "wrong code"
    tells a caller holding only a password whether the account has 2FA at all, which is a fact
    about someone else's account security.
    """

    pass


class TwoFactorEnrollmentError(Exception):
    """A confirmed device already exists (API §6.1 `409 CONFLICT`).

    Distinct from `TwoFactorError` because it maps to a different status code, and because it is
    the one 2FA failure that is safe to report specifically: only the account owner can reach it.
    """

    pass


def requires_two_factor(*, user: User) -> bool:
    """Whether `user` must clear a second factor before a full session is issued (FR-4).

    True when the account is under an Admin-set policy (`require_two_factor`, T1.6) **or** the
    user has voluntarily confirmed a TOTP device. FR-4 is a SHOULD — "optional 2FA for
    authorities/admins" — so enrolment alone opts an account in; the flag makes it mandatory.

    ⚠️ **The policy flag counts even with no device enrolled.** That combination is a login the
    user cannot complete, and it must stay that way: treating "flag set but no device" as "2FA
    not required" would let an Admin believe an account is protected while a password alone
    opens it. The escape is `POST /auth/2fa/enroll`, which accepts a partial session precisely
    so such an account can enrol its way out (API §6.1).

    [doc: API §6.1, FR-4, PRD §127]
    """
    return bool(user.require_two_factor) or _confirmed_device_for(user) is not None


def _confirmed_device_for(user: User) -> TOTPDevice | None:
    """The user's confirmed TOTP device, or `None`.

    ⚠️ Filters on `confirmed=True` explicitly. `TOTPDevice.objects.devices_for_user()` includes
    unconfirmed devices by default, so an abandoned half-finished enrolment would count as 2FA
    being active and lock the account out of its own login.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device: TOTPDevice | None = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    return device


def start_partial_session(*, request: HttpRequest, user: User) -> None:
    """Record a passed password check without authenticating the session (T1.7).

    Called by `LoginView` in place of `start_session()` when `requires_two_factor()` is true.
    The caller receives a normal session cookie, but the session behind it authenticates
    nothing — see the section header above.

    ⚠️ **`cycle_key()` is called explicitly here.** `start_session()` gets key rotation for free
    from `django_login()`; this path does not call it, so without this line a session token
    planted in the victim's browser before login would still be the token carrying the partial
    credential — session fixation, one factor earlier than usual. The full session issued after
    2FA rotates the key a second time (`django_login()` again), which is correct: the partial
    key has by then been transmitted and should not survive the upgrade.

    ⚠️ **`set_expiry` is per-session, not the global `SESSION_COOKIE_AGE`.** It is reset by
    `django_login()` on the way to a full session — Django's `login()` calls `cycle_key()`,
    which preserves the session data but the subsequent full-session write uses the global
    default. A partial session that is never completed simply expires.
    """
    request.session.cycle_key()
    request.session[PARTIAL_SESSION_USER_KEY] = str(user.pk)
    request.session.set_expiry(PARTIAL_SESSION_TTL_SECONDS)


def resolve_partial_session_user(*, request: HttpRequest) -> User:
    """Return the user behind a partial session, re-checking that they may still sign in.

    Raises:
        AuthenticationError: No partial session, or it names a user who no longer exists.
        AccountLockedError: The account was suspended/deprovisioned between the password check
            and this call.

    ⚠️ **The user is re-fetched and `is_active` re-checked, not trusted from the session.** The
    password step happened on an earlier request; an Admin may have suspended the account in
    between, and BR-25's suspension is meaningless if a login already in flight completes
    anyway. This is the same reason `revoke_all_sessions()` exists.

    ⚠️ **An authenticated caller is not accepted here.** A full session means the second factor
    is already done; letting one through would make `/auth/2fa/verify` a way to re-issue a
    session from a session, which is a refresh token by another name (auth.md: "No refresh
    tokens. The concept does not exist here.").
    """
    from urbenmend.identity.models import User

    user_id = request.session.get(PARTIAL_SESSION_USER_KEY)
    if not user_id:
        raise AuthenticationError(_GENERIC_LOGIN_FAILURE)

    try:
        user = User.objects.get(pk=user_id)
    except (User.DoesNotExist, ValidationError, ValueError) as exc:
        # ValidationError/ValueError guard a malformed UUID in a tampered session payload —
        # `get(pk=...)` raises on the cast before it ever queries.
        raise AuthenticationError(_GENERIC_LOGIN_FAILURE) from exc

    if not user.is_active:
        raise AccountLockedError("This account is not permitted to sign in.")

    return user


def enroll_totp_device(*, user: User) -> tuple[TOTPDevice, str, str]:
    """Create an unconfirmed TOTP device for `user` and return it with its shared secret.

    Returns:
        `(device, secret, otpauth_uri)`. `secret` is base32 — the form an authenticator app
        accepts when the user types it in by hand. `otpauth_uri` embeds the same secret and is
        what a client renders as a QR code.

    Raises:
        TwoFactorEnrollmentError: A confirmed device already exists (API §6.1 `409`).

    ⚠️ **The secret is returned as a tuple element, never read back off the model.** Same
    reasoning as T1.2's verification code: a value that only ever travels as a return value
    cannot be leaked by passing the row to a serializer or a log formatter.

    ⚠️ **`device.key` is NOT the secret to publish — it is hex, and `config_url` is base32.**
    Handing out `device.key` produces a response whose `secret` and `otpauthUri` disagree: the
    QR code works, manual entry silently yields wrong codes forever. Both fields are derived
    from `bin_key` here so they cannot drift apart.

    ⚠️ **Unconfirmed devices are replaced, not accumulated.** Re-enrolling after abandoning a
    setup is normal (wrong app, lost the QR code); leaving the old rows behind would mean an
    old, possibly screenshotted secret still confirms. A *confirmed* device is never silently
    replaced — that is the `409`, because overwriting one would let anyone with a live session
    swap the second factor for their own.

    ⚠️ **No `require_role` check.** FR-4 makes 2FA available to authorities and admins by policy,
    but a citizen choosing to protect their account is not a privilege escalation and refusing
    it would be a downgrade for no benefit. Authorization is "self", enforced structurally: the
    caller can only ever pass their own user (API §6.1).
    """
    from base64 import b32encode

    from django_otp.plugins.otp_totp.models import TOTPDevice

    if _confirmed_device_for(user) is not None:
        raise TwoFactorEnrollmentError(
            "This account already has a confirmed authenticator. Remove it before enrolling "
            "another."
        )

    with transaction.atomic():
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        device = TOTPDevice.objects.create(user=user, name="default", confirmed=False)

    # Exactly what `config_url` encodes, so the two forms of the secret cannot disagree.
    secret = b32encode(device.bin_key).decode("ascii")
    return device, secret, str(device.config_url)


def verify_totp(*, user: User, code: str) -> TOTPDevice:
    """Check a TOTP code against the user's device, confirming it if this is enrolment (T1.7).

    Returns:
        The device that accepted the code, now confirmed.

    Raises:
        TwoFactorError: No device enrolled, or the code is wrong, expired, or replayed.

    ⚠️ **A confirmed device is preferred over an unconfirmed one.** Checking the unconfirmed
    device first would let someone who reached a live session enrol a device of their own and
    then authenticate against it, sidestepping the `409` in `enroll_totp_device()`.

    ⚠️ **`device.verify_token()` does the work, and it must not be reimplemented.** It applies
    the drift tolerance, and — the part that matters — it stores `last_t` so a code cannot be
    replayed within its own validity window. A hand-rolled `pyotp`-style comparison looks
    equivalent and silently accepts the same code repeatedly for 30+ seconds, which is exactly
    the window an attacker who shoulder-surfed or intercepted one code needs. django-otp also
    applies its own per-device throttle on repeated failures; endpoint-level rate limiting is
    T1.8 and is a separate control.

    ⚠️ **Confirmation is a save of `confirmed=True`, not a second endpoint.** The first valid
    code proves the enrolling client holds the secret — that is the entire content of a
    confirmation step (API §6.1, amended 2026-08-07).
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device = _confirmed_device_for(user)
    if device is None:
        device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        raise TwoFactorError(_GENERIC_TWO_FACTOR_FAILURE)

    if not device.verify_token(code):
        raise TwoFactorError(_GENERIC_TWO_FACTOR_FAILURE)

    if not device.confirmed:
        device.confirmed = True
        device.save(update_fields=["confirmed"])

    return device
