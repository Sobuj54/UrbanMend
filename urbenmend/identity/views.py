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

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema
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
from urbenmend.api.pagination import StandardCursorPagination
from urbenmend.api.throttling import (
    AuthAnonRateThrottle,
    AuthIdentityRateThrottle,
    AuthUserRateThrottle,
    RateLimitHeadersMixin,
    clear_identity_throttle,
)
from urbenmend.identity import selectors, services
from urbenmend.identity.models import Channel, User
from urbenmend.identity.serializers import (
    AdminUserListQuerySerializer,
    AdminUserUpdateSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    PasswordForgotSerializer,
    PasswordResetSerializer,
    ProfileUpdateSerializer,
    ProvisionAuthoritySerializer,
    RegisterResponseSerializer,
    RegisterSerializer,
    TwoFactorEnrollResponseSerializer,
    TwoFactorVerifyResponseSerializer,
    TwoFactorVerifySerializer,
    UserSerializer,
    VerifyRequestSerializer,
    VerifyResponseSerializer,
)


class PasswordForgotView(RateLimitHeadersMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes = [AuthAnonRateThrottle]

    @extend_schema(
        request=PasswordForgotSerializer,
        responses={
            202: OpenApiResponse(description="Request accepted; response is always generic.")
        },
        tags=["Authentication"],
        operation_id="passwordForgot",
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordForgotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.request_password_reset(identifier=serializer.validated_data["identifier"])
        return Response(status=status.HTTP_202_ACCEPTED)


class PasswordResetView(RateLimitHeadersMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes = [AuthAnonRateThrottle]

    @extend_schema(
        request=PasswordResetSerializer,
        responses={
            200: OpenApiResponse(description="Password changed successfully."),
            422: OpenApiResponse(description="Reset token invalid or expired."),
        },
        tags=["Authentication"],
        operation_id="passwordReset",
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.reset_password(**serializer.validated_data)
        except DjangoValidationError as exc:
            raise UnprocessableEntity(exc.messages[0]) from exc
        return Response(status=status.HTTP_200_OK)


class RegisterView(RateLimitHeadersMixin, APIView):
    """`POST /auth/register` — register a citizen account (FR-1, API §6.1)."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    # Per-IP only: registration has no identifier to key on and no session, so `auth_anon` is the
    # only bucket that applies. Caps automated account creation from one source (T1.8).
    throttle_classes = [AuthAnonRateThrottle]

    @extend_schema(
        request=RegisterSerializer,
        responses={
            201: RegisterResponseSerializer,
            409: OpenApiResponse(description="Account conflict."),
        },
        tags=["Authentication"],
        operation_id="register",
    )
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


class VerifyView(RateLimitHeadersMixin, APIView):
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

    # ⚠️ Both buckets, because this endpoint serves both caller kinds (see the docstring). The
    # per-code `MAX_ATTEMPTS=5` in `verify_code()` (T1.2) caps guesses against *one* code; these
    # cap the volume of attempts across many codes and accounts, which that counter cannot see.
    throttle_classes = [AuthAnonRateThrottle, AuthUserRateThrottle]

    @extend_schema(
        request=VerifyRequestSerializer,
        responses={
            200: VerifyResponseSerializer,
            422: OpenApiResponse(description="Code invalid or expired."),
        },
        tags=["Authentication"],
        operation_id="verifyChannel",
    )
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


class LoginView(RateLimitHeadersMixin, APIView):
    """`POST /auth/login` — start a session (FR-1, API §6.1)."""

    permission_classes = [AllowAny]
    # ⚠️ Authentication is disabled on this view, which also disables DRF's
    # `SessionAuthentication` CSRF enforcement for it. That is correct and not a hole: there
    # is no session to protect yet, and Django's `CsrfViewMiddleware` still guards the
    # request. Leaving `SessionAuthentication` on would demand a CSRF token from a caller
    # who has never been issued one.
    authentication_classes: list[Any] = []
    # ⚠️ **The FR-4 brute-force limit lives here**, and the per-identifier bucket is the one that
    # matters: it caps guesses against a single account, which the per-IP bucket cannot do on its
    # own (an attacker rotating through proxies gets a fresh IP bucket each time, but the
    # identifier they are attacking stays the same). Both apply; whichever is tighter binds.
    throttle_classes = [AuthIdentityRateThrottle, AuthAnonRateThrottle]

    @extend_schema(
        request=LoginSerializer,
        responses={
            200: LoginResponseSerializer,
            401: OpenApiResponse(description="Invalid credentials."),
        },
        tags=["Authentication"],
        operation_id="login",
    )
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

        # ⚠️ Only reachable past every `raise` above, which is the entire contract of this call
        # (T1.8). Moving it earlier — or into a `finally` — erases the failed-attempt history the
        # throttle is counting, and every existing test would still pass because the happy path
        # never observes it. The per-IP bucket is deliberately left alone: see the docstring.
        clear_identity_throttle(request=request)

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


class UserCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        params = AdminUserListQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        values = params.validated_data
        queryset = selectors.list_users(
            actor=cast("User", request.user),
            role=values.get("role"),
            status=values.get("status"),
            query=values.get("q", ""),
        )
        paginator = StandardCursorPagination()
        page = paginator.paginate_queryset(queryset, request, view=self) or []
        return paginator.get_paginated_response(UserSerializer(page, many=True).data)


class UserAdminDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, user_id) -> Response:
        serializer = AdminUserUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            target = services.update_user_by_admin(
                actor=cast("User", request.user), user_id=user_id, **serializer.validated_data
            )
        except DjangoValidationError as exc:
            raise UnprocessableEntity(exc.messages[0]) from exc
        return Response(UserSerializer(target).data)


class TwoFactorEnrollView(RateLimitHeadersMixin, APIView):
    """`POST /auth/2fa/enroll` — begin TOTP enrolment (FR-4, API §6.1 amended 2026-08-07).

    ⚠️ **`AllowAny`, and the reason matters.** A partial session is *not* an authenticated
    request — `request.user` is `AnonymousUser` by design (services.py T1.7 header) — so
    `IsAuthenticated` would reject exactly the caller who needs this endpoint most: an account an
    Admin flagged `require_two_factor` that has no device and therefore cannot complete login.
    Authorization is not skipped, it moves into `_resolve_caller()`: one of the two session kinds
    must be present, and both name the user rather than accepting one from the body.
    """

    permission_classes = [AllowAny]
    # Both buckets: a partial-session caller is unauthenticated, so only `auth_anon` would apply
    # to them, while a full-session caller lands in `auth_user`. Registering both means neither
    # caller kind reaches this endpoint unlimited.
    throttle_classes = [AuthAnonRateThrottle, AuthUserRateThrottle]

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


class TwoFactorVerifyView(RateLimitHeadersMixin, APIView):
    """`POST /auth/2fa/verify` — complete 2FA, or confirm a new device (FR-4, API §6.1).

    Two callers reach this endpoint and both are correct: a partial session completing a login,
    and a full session confirming an enrolment it just started. `_resolve_caller()` accepts
    either; the service does not care which, because the check is the same either way.
    """

    permission_classes = [AllowAny]
    # ⚠️ **The "OTP" half of T1.8's "login/OTP rate limiting".** A TOTP code is six digits with a
    # 30-second window, so an unthrottled endpoint is brute-forceable outright — `verify_token()`
    # blocks *replay* of a code, not a walk through the keyspace. There is no per-account attempt
    # counter here the way `verify_code()` has one (T1.2), so these buckets are the only cap.
    #
    # ⚠️ A partial session is unauthenticated by T1.7's design, so the guessing path keys on IP via
    # `auth_anon`. That is the bucket to tighten if this ever needs to be stricter — narrowing
    # `auth_user` would only constrain callers who have already passed both factors.
    throttle_classes = [AuthAnonRateThrottle, AuthUserRateThrottle]

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


class MeView(RateLimitHeadersMixin, APIView):
    """`/users/me` — read, update, or anonymize the caller's own account (API §6.2, T1.9).

    Three methods on one view because all three share the same authorization: API §6.2 gives each
    of them "Auth: Session. Authorization: Self." That "self" is **structural** — the account comes
    from `request.user`, and no method reads an id from the path or the body, so there is no
    selector to tamper with and nothing for a scope check to compare (FR-3 is satisfied by the
    absence of a target parameter, not by an omitted check).

    ⚠️ **`GET /users` and `PATCH /users/{id}` are NOT here.** Both are Admin endpoints in API §6.2
    and both are out of T1.9's scope; `api/urls.py` records them as unowned rather than leaving
    them silently absent.
    """

    permission_classes = [IsAuthenticated]
    # ⚠️ `auth_user` only. `api-conventions.md`: "All protected endpoints implicitly return 401 and
    # 429". The per-IP bucket is deliberately absent — it is sized for pre-session auth attempts,
    # and applying it here would let one user's profile edits exhaust the login allowance for
    # everyone else behind the same NAT.
    throttle_classes = [AuthUserRateThrottle]

    def get(self, request: Request) -> Response:
        """API §6.2 `GET /users/me` — the caller's profile, one shape for all three roles."""
        return Response(
            UserSerializer(cast("User", request.user)).data,
            status=status.HTTP_200_OK,
        )

    def patch(self, request: Request) -> Response:
        """API §6.2 `PATCH /users/me` — update own phone and/or language.

        ⚠️ `partial=True` is **not** set and must not be. This is a plain `Serializer`, not a
        `ModelSerializer`, and every field on it is already `required=False`; `partial=True` would
        additionally suppress the empty-body and unknown-field rejections `validate()` performs,
        turning `PATCH {"role":"admin"}` back into a silent `200`.
        """
        serializer = ProfileUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            user = services.update_profile(
                user=cast("User", request.user),
                # ⚠️ `.get()` with no default, so an omitted field stays `None` — which the service
                # reads as "not submitted". A `""` default here would turn every language-only
                # update into a phone deletion.
                phone=data.get("phone"),
                preferred_language=data.get("preferred_language"),
            )
        except services.ProfileUpdateError as exc:
            # API §6.2 lists `409` (identity in use) for this endpoint. The message is specific:
            # the caller is authenticated and editing their own profile, so naming the collision
            # tells them nothing a registration attempt would not, and withholding it leaves them
            # unable to tell a typo from a genuine conflict.
            raise Conflict(str(exc)) from exc
        except DjangoValidationError as exc:
            # Clearing the last contact channel is a business-rule rejection, so `422` — not the
            # `400` a malformed body gets (api-conventions.md status table).
            raise UnprocessableEntity(exc.messages[0]) from exc

        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)

    def delete(self, request: Request) -> Response:
        """API §6.2 `DELETE /users/me` — anonymize the account (P6, BR-33, C-14).

        ⚠️ **`202`, and the session is already revoked when it is written.** The service revokes
        every session inside its transaction, so this response goes back to a credential that no
        longer authenticates. The `202` reflects that retained-record anonymization may extend past
        the response (P2/P3 add Reports and media) — not that the account is still usable.

        ⚠️ **No confirmation parameter, and no undo.** The spec defines neither; inventing a
        `?confirm=true` would be a contract change made in a view.
        """
        try:
            services.anonymize_account(user=cast("User", request.user))
        except services.AccountDeletionError as exc:
            # `403 FORBIDDEN` — an Authority or Admin may not self-delete (spec amended
            # 2026-08-07). Raised as `PermissionDenied` so the T0.6 handler renders the generic
            # `FORBIDDEN` code from `_STATUS_TO_CODE`; a bespoke code here would invent one the
            # spec does not list for this endpoint (contrast `ACCOUNT_LOCKED`, which §6.1 names).
            raise PermissionDenied(str(exc)) from exc

        # ⚠️ No body. The profile is exactly what was just anonymized, so echoing it would return
        # the PII this endpoint exists to erase.
        return Response(status=status.HTTP_202_ACCEPTED)


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
