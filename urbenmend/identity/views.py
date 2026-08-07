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

from typing import Any

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
    RegisterSerializer,
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
        services.start_session(request=request._request, user=user)

        return Response(
            LoginResponseSerializer(user).data,
            status=status.HTTP_200_OK,
        )


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
