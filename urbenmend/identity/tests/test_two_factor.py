"""
T1.7 — two-factor authentication for authority/admin (FR-4, API §6.1).

⚠️ The tests that matter most here are the ones asserting a **partial session grants nothing**.
The whole design rests on `_auth_user_id` being unset, so every other endpoint fails closed with
no per-view gate to forget. If that stops being true, `test_partial_session_is_rejected_*` is
what catches it — a suite that only proved "right code logs you in" would pass while a
password-only cookie opened the API.

Layered per `testing.md`: service tests own the rules, view tests own the contract.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from urbenmend.identity import services
from urbenmend.identity.models import Role, User, UserStatus

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-1"


def _user(**overrides: object) -> User:
    fields: dict[str, object] = {
        "email": "authority@example.test",
        "password": PASSWORD,
        "status": UserStatus.ACTIVE,
        "role": Role.AUTHORITY,
    }
    fields.update(overrides)
    return User.objects.create_user(**fields)  # type: ignore[arg-type]


def _current_code(device: TOTPDevice) -> str:
    """The code a correctly-configured authenticator app would show right now.

    ⚠️ Derived from the device's own key via django-otp's `TOTP`, not hardcoded and not read
    back through `verify_token`. A test that asked the device to validate a code it generated
    itself would pass against a broken implementation.
    """
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return f"{totp.token():0{device.digits}d}"


def _enrolled(user: User) -> TOTPDevice:
    """A device already confirmed, created directly.

    ⚠️ Deliberately does NOT go enrol → verify. Confirming through the service consumes the
    current time step (`verify_token` stores `last_t`), so any test that then generated a code
    inside the same 30-second window would get a *correct* replay rejection and read as a
    failure of whatever it was actually testing. Tests that need the real enrolment flow call
    `enroll_totp_device()` themselves and spend the code once.
    """
    return TOTPDevice.objects.create(user=user, name="default", confirmed=True)


# ---------------------------------------------------------------------------------------
# requires_two_factor — the policy gate
# ---------------------------------------------------------------------------------------


def test_no_flag_and_no_device_requires_nothing() -> None:
    assert services.requires_two_factor(user=_user()) is False


def test_admin_set_flag_requires_two_factor_even_with_no_device() -> None:
    """⚠️ The lockout case, asserted deliberately.

    An Admin flagged the account (T1.6) and no device exists yet. This MUST read `True` — the
    alternative is telling an Admin an account is protected while a password alone opens it.
    `POST /auth/2fa/enroll` accepting a partial session is what lets such an account recover.
    """
    assert services.requires_two_factor(user=_user(require_two_factor=True)) is True


def test_a_confirmed_device_opts_the_account_in_without_the_flag() -> None:
    """FR-4 is a SHOULD — voluntary enrolment counts, the flag only makes it mandatory."""
    user = _user()
    _enrolled(user)
    assert services.requires_two_factor(user=user) is True


def test_an_unconfirmed_device_does_not_opt_the_account_in() -> None:
    """⚠️ An abandoned half-finished enrolment must not lock the user out of their own login."""
    user = _user()
    services.enroll_totp_device(user=user)
    assert services.requires_two_factor(user=user) is False


# ---------------------------------------------------------------------------------------
# enroll_totp_device / verify_totp — service layer
# ---------------------------------------------------------------------------------------


def test_enrolment_creates_an_unconfirmed_device_and_returns_a_uri() -> None:
    user = _user()
    device, secret, uri = services.enroll_totp_device(user=user)

    assert device.confirmed is False
    assert uri.startswith("otpauth://totp/")


def test_the_returned_secret_is_the_one_embedded_in_the_uri() -> None:
    """⚠️ Regression guard: `device.key` is hex, `config_url` is base32.

    Publishing the hex key gives a response whose `secret` and `otpauthUri` disagree — the QR
    code works and manual entry silently produces wrong codes forever. Nothing else in the
    system would notice.
    """
    _device, secret, uri = services.enroll_totp_device(user=_user())

    assert f"secret={secret}" in uri


def test_re_enrolling_replaces_an_abandoned_unconfirmed_device() -> None:
    """⚠️ An old, possibly screenshotted secret must stop working, not accumulate."""
    user = _user()
    first, _s1, _u1 = services.enroll_totp_device(user=user)
    second, _s2, _u2 = services.enroll_totp_device(user=user)

    assert TOTPDevice.objects.filter(user=user).count() == 1
    assert first.key != second.key
    assert not TOTPDevice.objects.filter(pk=first.pk).exists()


def test_enrolling_over_a_confirmed_device_is_refused() -> None:
    """Anyone with a live session could otherwise swap the second factor for their own."""
    user = _user()
    _enrolled(user)

    with pytest.raises(services.TwoFactorEnrollmentError):
        services.enroll_totp_device(user=user)


def test_the_first_valid_code_confirms_an_enrolment() -> None:
    user = _user()
    device, _secret, _uri = services.enroll_totp_device(user=user)

    services.verify_totp(user=user, code=_current_code(device))

    device.refresh_from_db()
    assert device.confirmed is True


def test_verify_rejects_a_wrong_code() -> None:
    user = _user()
    services.enroll_totp_device(user=user)

    with pytest.raises(services.TwoFactorError):
        services.verify_totp(user=user, code="000000")


def test_verify_rejects_a_replayed_code() -> None:
    """⚠️ The reason `verify_token()` must not be reimplemented.

    A hand-rolled comparison accepts the same code for the whole 30-second window — exactly what
    an attacker who intercepted one code needs. django-otp stores `last_t`; this proves it.
    """
    user = _user()
    device = _enrolled(user)
    code = _current_code(device)
    services.verify_totp(user=user, code=code)

    with pytest.raises(services.TwoFactorError):
        services.verify_totp(user=user, code=code)


def test_verify_with_no_device_enrolled_fails() -> None:
    with pytest.raises(services.TwoFactorError):
        services.verify_totp(user=_user(), code="123456")


def test_every_verify_failure_reads_identically() -> None:
    """No-device and wrong-code must not be distinguishable on a pre-identity session."""
    no_device = _user(email="a@example.test")
    wrong_code = _user(email="b@example.test")
    services.enroll_totp_device(user=wrong_code)

    with pytest.raises(services.TwoFactorError) as no_device_exc:
        services.verify_totp(user=no_device, code="123456")
    with pytest.raises(services.TwoFactorError) as wrong_code_exc:
        services.verify_totp(user=wrong_code, code="000000")

    assert str(no_device_exc.value) == str(wrong_code_exc.value)


def test_a_confirmed_device_is_preferred_over_an_unconfirmed_one() -> None:
    """Otherwise a caller could enrol their own device and authenticate against it."""
    user = _user()
    confirmed = _enrolled(user)
    planted = TOTPDevice.objects.create(user=user, name="planted", confirmed=False)

    services.verify_totp(user=user, code=_current_code(confirmed))

    planted.refresh_from_db()
    assert planted.confirmed is False


# ---------------------------------------------------------------------------------------
# The partial session — what it does NOT grant
# ---------------------------------------------------------------------------------------


def _login(client: Client, user: User) -> object:
    return client.post(
        reverse("api:auth-login"),
        data={"identifier": user.email, "password": PASSWORD},
        content_type="application/json",
    )


def test_login_with_2fa_pending_returns_requires2fa_and_omits_the_user(client: Client) -> None:
    user = _user(require_two_factor=True)

    response = _login(client, user)

    assert response.status_code == 200  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert body["requires2fa"] is True
    # ⚠️ Omitted, not null — the role is the fact worth withholding from a password-only holder.
    assert "user" not in body


def test_login_without_2fa_still_returns_the_user(client: Client) -> None:
    """The T1.3 contract must survive T1.7 unchanged for accounts with no second factor."""
    user = _user()

    body = _login(client, user).json()  # type: ignore[attr-defined]

    assert body["requires2fa"] is False
    assert body["user"]["id"] == str(user.id)


def test_partial_session_does_not_authenticate_the_request(client: Client) -> None:
    """⚠️ The core assertion of T1.7.

    `_auth_user_id` is never written, so `request.user` stays `AnonymousUser` and every
    authenticated endpoint rejects the cookie — with no per-view check that a future view could
    forget to add.
    """
    from django.contrib.auth import SESSION_KEY

    _login(client, _user(require_two_factor=True))

    assert SESSION_KEY not in client.session
    assert services.PARTIAL_SESSION_USER_KEY in client.session


def test_partial_session_is_rejected_by_an_authenticated_endpoint(client: Client) -> None:
    """A password-only cookie must not open anything but `/auth/2fa/*`."""
    _login(client, _user(require_two_factor=True))

    response = client.post(reverse("api:auth-logout"))

    assert response.status_code == 401


def test_partial_session_cannot_provision_an_authority(client: Client) -> None:
    """The same check against an Admin-only endpoint, since role is what 2FA protects here."""
    admin = _user(email="admin@example.test", role=Role.ADMIN, require_two_factor=True)
    _login(client, admin)

    response = client.post(
        reverse("api:users-authorities"),
        data={"email": "new@example.test", "categoryScope": ["roads"]},
        content_type="application/json",
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------------------
# /auth/2fa/enroll + /auth/2fa/verify — the contract
# ---------------------------------------------------------------------------------------


def test_enroll_returns_201_with_the_spec_shape(client: Client) -> None:
    user = _user()
    client.force_login(user)

    response = client.post(reverse("api:auth-2fa-enroll"))

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"secret", "otpauthUri", "confirmed"}
    assert body["confirmed"] is False
    assert body["otpauthUri"].startswith("otpauth://totp/")


def test_enroll_is_reachable_on_a_partial_session(client: Client) -> None:
    """⚠️ The lockout escape hatch (API §6.1).

    An account flagged `require_two_factor` with no device cannot complete login, so enrolment
    has to accept the partial session or the account is permanently locked out.
    """
    _login(client, _user(require_two_factor=True))

    response = client.post(reverse("api:auth-2fa-enroll"))

    assert response.status_code == 201


def test_enroll_without_any_session_returns_401(client: Client) -> None:
    assert client.post(reverse("api:auth-2fa-enroll")).status_code == 401


def test_enroll_over_a_confirmed_device_returns_409(client: Client) -> None:
    user = _user()
    _enrolled(user)
    client.force_login(user)

    response = client.post(reverse("api:auth-2fa-enroll"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_verify_completes_login_and_upgrades_to_a_full_session(client: Client) -> None:
    from django.contrib.auth import SESSION_KEY

    user = _user()
    device = _enrolled(user)
    client.logout()
    _login(client, user)

    response = client.post(
        reverse("api:auth-2fa-verify"),
        data={"code": _current_code(device)},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user.id)
    assert client.session[SESSION_KEY] == str(user.pk)


def test_verify_rotates_the_partial_session_key(client: Client) -> None:
    """⚠️ The token that carried a password-only credential must not survive the upgrade."""
    user = _user()
    device = _enrolled(user)
    client.logout()
    _login(client, user)
    partial_key = client.session.session_key

    client.post(
        reverse("api:auth-2fa-verify"),
        data={"code": _current_code(device)},
        content_type="application/json",
    )

    assert client.session.session_key != partial_key


def test_verify_with_a_wrong_code_returns_422_and_grants_no_session(client: Client) -> None:
    from django.contrib.auth import SESSION_KEY

    user = _user()
    _enrolled(user)
    client.logout()
    _login(client, user)

    response = client.post(
        reverse("api:auth-2fa-verify"),
        data={"code": "000000"},
        content_type="application/json",
    )

    assert response.status_code == 422
    assert SESSION_KEY not in client.session


def test_verify_without_any_session_returns_401(client: Client) -> None:
    response = client.post(
        reverse("api:auth-2fa-verify"),
        data={"code": "123456"},
        content_type="application/json",
    )

    assert response.status_code == 401


def test_verify_confirms_an_enrolment_on_a_full_session(client: Client) -> None:
    """The second caller the endpoint serves: confirming a device just enrolled."""
    user = _user()
    device, _secret, _uri = services.enroll_totp_device(user=user)
    client.force_login(user)

    response = client.post(
        reverse("api:auth-2fa-verify"),
        data={"code": _current_code(device)},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["confirmed"] is True
    device.refresh_from_db()
    assert device.confirmed is True


def test_an_account_suspended_mid_login_cannot_finish(client: Client) -> None:
    """⚠️ BR-25 suspension must stop a login already in flight, not just future ones."""
    user = _user()
    device = _enrolled(user)
    client.logout()
    _login(client, user)

    User.objects.filter(pk=user.pk).update(status=UserStatus.SUSPENDED)

    response = client.post(
        reverse("api:auth-2fa-verify"),
        data={"code": _current_code(device)},
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_LOCKED"
