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
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbenmend.identity.models import Channel, User, VerificationCode


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
