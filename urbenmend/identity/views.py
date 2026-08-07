"""
Identity & Access — HTTP layer (T1.2).

⚠️ Views are thin by mandate [doc: workflow §B7, FR-3, R-12]. Every rule below lives in
`services.py`; this module parses the request, calls one service function, and shapes the
response. A rule that appears here is a defect, not a shortcut — DRF permission classes are
defence-in-depth, never the enforcement point.

Service exceptions are translated to the API §4.1 error envelope by raising DRF exceptions
with the spec's `code`; the T0.6 handler renders them.

[doc: API §6.1 /auth]
"""

from __future__ import annotations

from typing import Any, cast

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from urbenmend.api.exceptions import (
    AccountLocked,
    Conflict,
    InvalidCredentials,
    UnprocessableEntity,
)
from urbenmend.identity import services
from urbenmend.identity.models import Channel, User
from urbenmend.identity.serializers import (
    LoginResponseSerializer,
    LoginSerializer,
    ProvisionAuthoritySerializer,
    RegisterSerializer,
    TwoFactorEnrollResponseSerializer,
    TwoFactorVerifyResponseSerializer,
    TwoFactorVerifySerializer,
    UserSerializer,
    VerifyRequestSerializer,
)


class RegisterView(APIView):
    """`POST /auth/register` — register a citizen account (FR-1, API §6.1)."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []

    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            user = services.register_citizen(
                email=data.get("email"),
                phone=data.get("phone"),
                password=data["password"],
                preferred_language=data.get("preferred_language", "en"),
            )
        except services.RegistrationError as exc:
            raise Conflict(str(exc)) from exc
        except DjangoValidationError:
            raise

        # Which channels need verifying — exactly the ones the account actually has.
        channels = [
            channel
            for channel, present in (("email", user.email), ("phone", user.phone))
            if present
        ]

        for channel in channels:
            # ⚠️ The plaintext code is deliberately discarded here. It must never reach a
            # response body — the whole point of the channel is that only someone in control
            # of the mailbox or handset can read it. Delivery is Q5's to wire up.
            services.send_verification_code(user=user, channel=Channel(channel))

        return Response(
            {
                "userId": str(user.id),
                "verificationRequired": True,
                "channels": channels,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyView(APIView):
    """`POST /auth/verify` — confirm a channel with the emailed/texted code (API §6.1).

    API §6.1: "Auth: None (pre-session) or session." An authenticated user is verifying an
    additional channel; an unauthenticated user is completing initial registration verification.
    The unauthenticated path requires an `identifier` in the body to look up the user.
    """

    permission_classes = [AllowAny]

    # ⚠️ One message for every pre-session failure. Distinguishing "no such account" from
    # "wrong code" would make this endpoint an enumeration oracle: it is unauthenticated, so
    # anyone could probe addresses and read the difference in the reply
    # [doc: api-conventions.md "no user enumeration", auth.md].
    _GENERIC_FAILURE = "The verification code is invalid or has expired."

    def post(self, request: Request) -> Response:
        serializer = VerifyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # API §6.1: "Auth: None (pre-session) or session."
        if request.user.is_authenticated:
            user = request.user
        else:
            identifier = data.get("identifier")
            if not identifier:
                raise UnprocessableEntity(self._GENERIC_FAILURE)

            # ⚠️ Normalized the same way `User.save()` normalizes on the way in, or a user
            # who registered as `Citizen@Example.test` (stored lowercased) could never verify
            # by typing it back the way they wrote it.
            identifier = identifier.strip()
            try:
                if "@" in identifier:
                    user = User.objects.get(email=identifier.lower())
                else:
                    user = User.objects.get(phone=identifier)
            except User.DoesNotExist as exc:
                raise UnprocessableEntity(self._GENERIC_FAILURE) from exc

        try:
            services.verify_code(
                user=user,
                channel=Channel(data["channel"]),
                code=data["code"],
            )
        except services.VerificationError as exc:
            # ⚠️ The service's specific reason ("already used", "expired", "too many
            # attempts") is deliberately NOT forwarded on the pre-session path — each one
            # confirms the account exists. An authenticated caller has already proved who
            # they are, so the detail helps them and reveals nothing.
            if request.user.is_authenticated:
                raise UnprocessableEntity(str(exc)) from exc
            raise UnprocessableEntity(self._GENERIC_FAILURE) from exc

        return Response({"verified": True}, status=status.HTTP_200_OK)


class LoginView(APIView):
    """`POST /auth/login` — start a session (FR-1, API §6.1)."""

    permission_classes = [AllowAny]
    # ⚠️ Authentication is disabled on this view, which also disables DRF's
    # `SessionAuthentication` CSRF enforcement for it. That is correct and not a hole: there
    # is no session to protect yet, and Django's `CsrfViewMiddleware` still guards the
    # request. Leaving `SessionAuthentication` on would demand a CSRF token from a caller
    # who has never been issued one.
    authentication_classes: list[Any] = []

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            user = services.authenticate_user(
                identifier=data["identifier"],
                password=data["password"],
            )
        except services.AccountLockedError as exc:
            # ⚠️ Ordered before the `AuthenticationError` clause because it subclasses it —
            # reversing these two makes every locked account report `401` instead of
            # `403 ACCOUNT_LOCKED` (API §6.1), and nothing would fail loudly.
            raise AccountLocked(str(exc)) from exc
        except services.AuthenticationError as exc:
            raise InvalidCredentials(str(exc)) from exc

        # ⚠️ Session established only after `authenticate_user` returned. The service raises
        # on every failure path, so there is no arrangement of its results that reaches this
        # line without a verified password.
        #
        # ⚠️ **Which session depends on `requires_two_factor()`, and the serializer calls the
        # same function** (T1.7, FR-4). A full session here for an account that needs a second
        # factor would be the T1.3 trap: the body says `requires2fa: true` while access has
        # already been granted. One service function feeding both sides is what makes the
        # cookie and the body unable to disagree.
        if services.requires_two_factor(user=user):
            services.start_partial_session(request=request._request, user=user)
        else:
            services.start_session(request=request._request, user=user)

        return Response(
            LoginResponseSerializer(user).data,
            status=status.HTTP_200_OK,
        )


class ProvisionAuthorityView(APIView):
    """`POST /users/authorities` — provision an Authority with category scope (API §6.2).

    FR-2 (admin-provisioned, not self-serve), BR-25 (Admin-only, audited), BR-26 (the scope).

    ⚠️ **No `permission_classes` role check, and that is the point.** `IsAuthenticated` is the
    project default and stays; the Admin requirement is `require_role(actor, Role.ADMIN)` inside
    `provision_authority`. An `IsAdminUser` here would read as the enforcement point and drift
    from it — DRF's `IsAdminUser` checks `is_staff`, which is Django-admin plumbing, not the
    domain `role` column (FR-3, R-12).

    ⚠️ Nothing in this method catches `AuthorizationError`. It subclasses Django's
    `PermissionDenied`, which `urbenmend_exception_handler` already renders as `403 FORBIDDEN` —
    a local `except` would only be an opportunity to get the status code wrong.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = ProvisionAuthoritySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            authority = services.provision_authority(
                # ⚠️ `cast`, not a guard: `IsAuthenticated` has already run, so `request.user` is
                # a `User` and never `AnonymousUser` here — `test_unauthenticated_gets_401` is what
                # keeps that true. The cast narrows the type without weakening anything, because
                # `require_role()` re-checks `is_authenticated` at runtime regardless of what it
                # is handed; a service that trusted this annotation would be the actual defect.
                actor=cast("User", request.user),
                email=data.get("email"),
                phone=data.get("phone"),
                category_slugs=data["category_scope"],
                require_two_factor=data["require_two_factor"],
            )
        except services.ProvisioningError as exc:
            # API §6.2 lists `409` for this endpoint. Unlike registration, the message is
            # specific: the caller is an Admin who needs to know the address is taken.
            raise Conflict(str(exc)) from exc
        except DjangoValidationError as exc:
            # An unknown or retired category key — a business-rule rejection, so `422`, not the
            # `400` a malformed body would get (api-conventions.md status table).
            raise UnprocessableEntity(exc.messages[0]) from exc

        return Response(
            UserSerializer(authority).data,
            status=status.HTTP_201_CREATED,
        )


class TwoFactorEnrollView(APIView):
    """`POST /auth/2fa/enroll` — begin TOTP enrolment (FR-4, API §6.1 amended 2026-08-07).

    ⚠️ **`AllowAny`, and the reason matters.** A partial session is *not* an authenticated
    request — `request.user` is `AnonymousUser` by design (services.py T1.7 header) — so
    `IsAuthenticated` would reject exactly the caller who needs this endpoint most: an account an
    Admin flagged `require_two_factor` that has no device and therefore cannot complete login.
    Authorization is not skipped, it moves into `_resolve_caller()`: one of the two session kinds
    must be present, and both name the user rather than accepting one from the body.
    """

    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        user = _resolve_caller(request)

        try:
            _device, secret, otpauth_uri = services.enroll_totp_device(user=user)
        except services.TwoFactorEnrollmentError as exc:
            raise Conflict(str(exc)) from exc

        # ⚠️ Both values come from the service's return tuple, never off the device row — the
        # model is not handed to the serializer, so no django-otp field addition can widen this
        # response, and `secret` cannot accidentally become the hex `device.key`.
        return Response(
            TwoFactorEnrollResponseSerializer(
                {
                    "secret": secret,
                    "otpauth_uri": otpauth_uri,
                    "confirmed": False,
                }
            ).data,
            status=status.HTTP_201_CREATED,
        )


class TwoFactorVerifyView(APIView):
    """`POST /auth/2fa/verify` — complete 2FA, or confirm a new device (FR-4, API §6.1).

    Two callers reach this endpoint and both are correct: a partial session completing a login,
    and a full session confirming an enrolment it just started. `_resolve_caller()` accepts
    either; the service does not care which, because the check is the same either way.
    """

    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = TwoFactorVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        was_partial = not request.user.is_authenticated
        user = _resolve_caller(request)

        try:
            services.verify_totp(user=user, code=serializer.validated_data["code"])
        except services.TwoFactorError as exc:
            raise UnprocessableEntity(str(exc)) from exc

        # ⚠️ Upgrade to a full session only on the partial path, and only after `verify_totp()`
        # returned. `start_session()` wraps `django_login()`, whose `cycle_key()` rotates the
        # partial session key away — the token that carried a password-only credential does not
        # survive into the authenticated session. Re-running it for an already-authenticated
        # caller would be a session refresh, which auth.md rules out.
        if was_partial:
            services.start_session(request=request._request, user=user)

        return Response(
            TwoFactorVerifyResponseSerializer(user).data,
            status=status.HTTP_200_OK,
        )


def _resolve_caller(request: Request) -> User:
    """The user behind either a full session or a partial post-password one (T1.7).

    ⚠️ **A full session wins over a partial one.** Both keys can coexist — a partial session
    that completes keeps its data across `cycle_key()` — so preferring `request.user` avoids
    resolving a stale `PARTIAL_SESSION_USER_KEY` left over from the login that created it.

    ⚠️ Raises `InvalidCredentials` (`401`) rather than DRF's `NotAuthenticated`, matching the
    T1.3 note in CLAUDE.md: `handle_exception` special-cases the latter, and API §4.2 wants a
    plain `401 UNAUTHENTICATED` here.
    """
    if request.user.is_authenticated:
        # No `cast` needed — `is_authenticated` narrows `User | AnonymousUser` to `User`, and
        # mypy rejects a redundant one. `ProvisionAuthorityView` still needs its cast because
        # `IsAuthenticated` guarantees the same thing without narrowing the type.
        return request.user

    try:
        return services.resolve_partial_session_user(request=request._request)
    except services.AccountLockedError as exc:
        raise AccountLocked(str(exc)) from exc
    except services.AuthenticationError as exc:
        raise InvalidCredentials(str(exc)) from exc


class LogoutView(APIView):
    """`POST /auth/logout` — revoke the current session (API §6.1, Arch §8).

    API §6.1: "Auth: Session. Authorization: Self." A caller can only ever end the session
    they presented, so "self" is structural rather than a check the service performs.
    """

    # `IsAuthenticated` is the project default, restated here because this view's whole
    # contract is that it needs a session. It is defence-in-depth, not the enforcement
    # point — `end_session()` on a request with no session is a harmless no-op.
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        services.end_session(request=request._request)
        # API §6.1: `204`, no body.
        return Response(status=status.HTTP_204_NO_CONTENT)
