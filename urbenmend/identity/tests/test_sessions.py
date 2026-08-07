"""
T1.3 — server-validated sessions, login, logout, revocation (Arch §8, API §2/§6.1).

⚠️ The revocation tests are the point of this file, not an extra. Sessions were chosen over
JWT for exactly one reason — immediate server-side revocation (Arch §8) — so a suite that
proves login works but never proves a session can be killed has not tested the decision.
Workflow §C2 states the requirement directly: "delete the session row and assert the next
request returns 401".

Layered per `testing.md`: service tests own the rules, view tests own the contract.
"""

from __future__ import annotations

from importlib import import_module

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client
from django.urls import reverse

from urbenmend.identity import services
from urbenmend.identity.models import Role, User, UserStatus

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-1"


def _citizen(**overrides: object) -> User:
    """A citizen who can log in. `status` defaults to the post-verification state."""
    fields: dict[str, object] = {
        "email": "citizen@example.test",
        "password": PASSWORD,
        "status": UserStatus.ACTIVE,
        "role": Role.CITIZEN,
    }
    fields.update(overrides)
    return User.objects.create_user(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------
# authenticate_user — service layer
# ---------------------------------------------------------------------------------------


def test_authenticate_returns_the_user_on_correct_credentials() -> None:
    user = _citizen()

    assert services.authenticate_user(identifier="citizen@example.test", password=PASSWORD) == user


def test_authenticate_normalizes_email_case() -> None:
    """Registration lowercases on the way in, so login must lowercase on the way back."""
    user = _citizen(email="Citizen@Example.TEST")

    result = services.authenticate_user(identifier="Citizen@Example.TEST", password=PASSWORD)

    assert result == user


def test_authenticate_accepts_a_phone_identifier() -> None:
    user = _citizen(email=None, phone="+8801712345678")

    assert services.authenticate_user(identifier="+8801712345678", password=PASSWORD) == user


def test_authenticate_rejects_a_wrong_password() -> None:
    _citizen()

    with pytest.raises(services.AuthenticationError):
        services.authenticate_user(identifier="citizen@example.test", password="wrong-pass-9")


def test_authenticate_rejects_an_unknown_identifier() -> None:
    with pytest.raises(services.AuthenticationError):
        services.authenticate_user(identifier="stranger@example.test", password=PASSWORD)


def test_authenticate_gives_the_same_message_for_unknown_user_and_wrong_password() -> None:
    """⚠️ The no-enumeration rule at the service layer (API §6.1, api-conventions.md).

    Asserted here as well as at the view because the view forwards `str(exc)` — if the two
    service paths ever diverged, the view would faithfully leak the difference.
    """
    _citizen()

    with pytest.raises(services.AuthenticationError) as wrong_password:
        services.authenticate_user(identifier="citizen@example.test", password="wrong-pass-9")
    with pytest.raises(services.AuthenticationError) as unknown_user:
        services.authenticate_user(identifier="stranger@example.test", password=PASSWORD)

    assert str(wrong_password.value) == str(unknown_user.value)


@pytest.mark.parametrize("status", [UserStatus.SUSPENDED, UserStatus.DEPROVISIONED])
def test_authenticate_rejects_a_non_authenticating_status(status: UserStatus) -> None:
    _citizen(status=status)

    with pytest.raises(services.AccountLockedError):
        services.authenticate_user(identifier="citizen@example.test", password=PASSWORD)


@pytest.mark.parametrize("status", [UserStatus.REGISTERED, UserStatus.VERIFIED, UserStatus.ACTIVE])
def test_authenticate_allows_every_active_status(status: UserStatus) -> None:
    """An unverified account CAN sign in — BR-30 limits what it may then do, not whether."""
    _citizen(status=status)

    assert services.authenticate_user(identifier="citizen@example.test", password=PASSWORD)


def test_locked_account_error_is_caught_as_an_authentication_error() -> None:
    """⚠️ The fail-closed direction: `except AuthenticationError` must also catch locked.

    If `AccountLockedError` stopped subclassing `AuthenticationError`, a caller with only
    the broad `except` would fall through and treat a suspended account as authenticated.
    """
    assert issubclass(services.AccountLockedError, services.AuthenticationError)


def test_wrong_password_on_a_locked_account_reports_invalid_not_locked() -> None:
    """⚠️ Order matters: password first, status second.

    Reporting "locked" to someone who did not supply the password would confirm both that
    the account exists and that it has been suspended — to anyone who asked.
    """
    _citizen(status=UserStatus.SUSPENDED)

    with pytest.raises(services.AuthenticationError) as exc:
        services.authenticate_user(identifier="citizen@example.test", password="wrong-pass-9")

    assert not isinstance(exc.value, services.AccountLockedError)


# ---------------------------------------------------------------------------------------
# POST /auth/login — view layer (contract)
# ---------------------------------------------------------------------------------------


def test_login_endpoint_returns_200_with_the_spec_shape(client: Client) -> None:
    """API §6.1: `{user: {id, role, preferredLanguage}, requires2fa}`."""
    user = _citizen()

    response = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(user.id)
    assert body["user"]["role"] == Role.CITIZEN
    assert body["user"]["preferredLanguage"] == "en"
    assert body["requires2fa"] is False


def test_login_response_never_carries_contact_details(client: Client) -> None:
    """⚠️ API §2.1: the API never returns contact info. A login body is where it creeps in."""
    _citizen(phone="+8801712345678")

    response = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert set(response.json()["user"]) == {"id", "role", "preferredLanguage"}
    assert "citizen@example.test" not in response.content.decode()
    assert "+8801712345678" not in response.content.decode()


def test_login_endpoint_sets_a_session_cookie(client: Client) -> None:
    """API §2: an opaque session token in a Secure/HttpOnly/SameSite cookie."""
    _citizen()

    response = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie.value
    assert cookie["httponly"] is True
    assert cookie["samesite"] == settings.SESSION_COOKIE_SAMESITE
    # The cookie carries the session key, never anything derived from the password.
    assert PASSWORD not in cookie.value


def test_login_writes_a_server_side_session_row(client: Client) -> None:
    """⚠️ Server-validated, not stateless. If this row is absent the session is a JWT in
    disguise and revocation cannot work (Arch §8)."""
    _citizen()
    assert Session.objects.count() == 0

    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert Session.objects.count() == 1


def test_login_endpoint_returns_401_on_wrong_password(client: Client) -> None:
    _citizen()

    response = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": "wrong-pass-9"},
        content_type="application/json",
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_login_endpoint_does_not_leak_account_existence(client: Client) -> None:
    """⚠️ Unknown address and wrong password must be indistinguishable (API §6.1)."""
    _citizen()

    known = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": "wrong-pass-9"},
        content_type="application/json",
    )
    unknown = client.post(
        reverse("api:auth-login"),
        data={"identifier": "stranger@example.test", "password": "wrong-pass-9"},
        content_type="application/json",
    )

    assert known.status_code == unknown.status_code == 401
    assert known.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert known.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_login_endpoint_returns_403_account_locked_for_a_suspended_account(
    client: Client,
) -> None:
    """API §6.1: "423-equivalent surfaced as 403 ACCOUNT_LOCKED"."""
    _citizen(status=UserStatus.SUSPENDED)

    response = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert response.status_code == 403
    # ⚠️ Not the generic FORBIDDEN — a client cannot tell "retry" from "call support"
    # without this specific code.
    assert response.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_login_failure_establishes_no_session(client: Client) -> None:
    _citizen()

    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": "wrong-pass-9"},
        content_type="application/json",
    )

    assert Session.objects.count() == 0


def test_login_rotates_the_session_key(client: Client) -> None:
    """⚠️ Session-fixation defence (Arch §8). `login()` cycles the key; a hand-rolled
    `request.session[...]` assignment would not, leaving a pre-login token planted by an
    attacker valid afterwards.

    The pre-login session is planted directly rather than obtained by making a request:
    Django only sends the cookie when the session was *modified*, and an anonymous request
    that touches nothing never writes one. Planting is also the closer model of the attack —
    fixation means the victim's browser already holds an attacker-chosen key before they
    ever authenticate.
    """
    _citizen()
    planted = import_module(settings.SESSION_ENGINE).SessionStore()
    planted["fixation_probe"] = True
    planted.create()
    pre_login_key = planted.session_key
    client.cookies[settings.SESSION_COOKIE_NAME] = pre_login_key

    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert client.cookies[settings.SESSION_COOKIE_NAME].value != pre_login_key
    # ⚠️ The planted session is destroyed, not merely superseded. A key that still resolves
    # server-side remains a usable credential for whoever planted it, and the new cookie in
    # the victim's browser does nothing to stop them presenting the old one.
    assert not Session.objects.filter(session_key=pre_login_key).exists()


# ---------------------------------------------------------------------------------------
# POST /auth/logout — view layer (contract)
# ---------------------------------------------------------------------------------------


def test_logout_returns_204_and_destroys_the_session(client: Client) -> None:
    _citizen()
    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )
    assert Session.objects.count() == 1

    response = client.post(reverse("api:auth-logout"))

    assert response.status_code == 204
    assert response.content == b""
    # ⚠️ The row is gone, not merely the cookie. A client-side-only logout leaves a valid
    # session behind for anyone holding the token.
    assert Session.objects.count() == 0


def test_logout_without_a_session_returns_401(client: Client) -> None:
    response = client.post(reverse("api:auth-logout"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


# ---------------------------------------------------------------------------------------
# Revocation — the reason sessions were chosen over JWT (Arch §8)
# ---------------------------------------------------------------------------------------


def test_deleting_the_session_row_makes_the_next_request_401(client: Client) -> None:
    """⚠️ Workflow §C2's explicit T1.3 requirement, and the whole case for sessions.

    Deletes the session server-side while the client keeps its cookie, then asserts the
    next authenticated request fails. A stateless token would still be accepted here.
    """
    _citizen()
    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )
    # Confirm the session works before revoking, or the assertion below proves nothing.
    assert client.post(reverse("api:auth-logout")).status_code == 204

    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )
    session_key = client.cookies[settings.SESSION_COOKIE_NAME].value

    # ⚠️ Through the SessionStore, not `Session.objects.filter(...).delete()`. On the
    # cached_db backend a raw row delete leaves the cached copy live, and the request
    # below would still succeed — which is exactly the bug this test exists to catch.
    import_module(settings.SESSION_ENGINE).SessionStore(session_key=session_key).delete()

    assert client.post(reverse("api:auth-logout")).status_code == 401


def test_revoke_all_sessions_kills_every_session_for_that_user() -> None:
    """BR-25/BR-33: suspension and deprovisioning must stop live sessions immediately."""
    user = _citizen()
    # Two devices, two independent clients — a real user's phone and laptop.
    phone, laptop = Client(), Client()
    for device in (phone, laptop):
        device.post(
            reverse("api:auth-login"),
            data={"identifier": "citizen@example.test", "password": PASSWORD},
            content_type="application/json",
        )
    assert Session.objects.count() == 2

    revoked = services.revoke_all_sessions(user=user)

    assert revoked == 2
    assert phone.post(reverse("api:auth-logout")).status_code == 401
    assert laptop.post(reverse("api:auth-logout")).status_code == 401


def test_revoke_all_sessions_leaves_other_users_alone() -> None:
    """⚠️ Suspending one account must not sign out the entire user base."""
    target = _citizen()
    bystander = _citizen(email="other@example.test")

    target_client, bystander_client = Client(), Client()
    target_client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )
    bystander_client.post(
        reverse("api:auth-login"),
        data={"identifier": "other@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert services.revoke_all_sessions(user=target) == 1

    assert target_client.post(reverse("api:auth-logout")).status_code == 401
    # The bystander's session is untouched — still good for a 204.
    assert bystander_client.post(reverse("api:auth-logout")).status_code == 204
    assert bystander.pk is not None


def test_revoke_all_sessions_returns_zero_when_there_are_none() -> None:
    user = _citizen()

    assert services.revoke_all_sessions(user=user) == 0
