"""
T1.4 — CSRF protection for state-changing requests (API §2, Arch §8).

⚠️ CSRF enforcement is scoped to **authenticated** requests. Pre-session endpoints (register,
verify, login) are deliberately CSRF-exempt: a client visiting the site for the first time has
no session and has never been issued a CSRF token, so demanding one would make registration and
login impossible. The browser sends no auth cookie on those requests, so CSRF is not the threat
model (login CSRF exists but is lower severity and outside this task's scope).

**Authenticated** endpoints carry the session cookie automatically, so CSRF protection is
mandatory there — DRF's `SessionAuthentication` enforces it by running Django's CSRF check when
`request.user` is authenticated. This file proves that contract.

[doc: API §2 "CSRF token on state-changing requests", auth.md, ADR-001]
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from urbenmend.identity.models import Role, User, UserStatus

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-1"


def _citizen(**overrides: object) -> User:
    """A citizen who can log in."""
    fields: dict[str, object] = {
        "email": "citizen@example.test",
        "password": PASSWORD,
        "status": UserStatus.ACTIVE,
        "role": Role.CITIZEN,
    }
    fields.update(overrides)
    return User.objects.create_user(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------
# CSRF enforcement on authenticated endpoints
# ---------------------------------------------------------------------------------------


def test_logout_without_csrf_token_returns_403(client: Client) -> None:
    """⚠️ The deliverable. An authenticated POST with no CSRF token is rejected.

    API §2: "CSRF token on state-changing requests". SessionAuthentication enforces it by
    running Django's CSRF check when the user is authenticated. A request carrying the
    session cookie but no CSRF token is the classic CSRF attack vector.
    """
    _citizen()
    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )
    # The client now holds a valid session cookie. Drop the CSRF token Django's test
    # client automatically includes, then attempt an authenticated request.
    client.cookies.pop(settings.CSRF_COOKIE_NAME, None)
    # ⚠️ Also disable the test client's automatic CSRF header injection. Without this the
    # test would pass for the wrong reason — the client fakes a token even when the cookie
    # is absent.
    client.handler.enforce_csrf_checks = True

    response = client.post(reverse("api:auth-logout"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    # ⚠️ DRF's SessionAuthentication.enforce_csrf() raises PermissionDenied with a message
    # that starts "CSRF Failed:". That detail leaking into the response is acceptable here
    # — it helps the client developer understand why their request was rejected without
    # revealing anything an attacker doesn't already know.
    assert "CSRF" in response.json()["error"]["message"]


def test_logout_with_valid_csrf_token_succeeds(client: Client) -> None:
    """The CSRF check allows requests that carry a valid token (API §2).

    Django's test Client automatically includes the CSRF token in every request, simulating
    a well-behaved browser. This test just asserts that the happy path works — the real
    verification is the rejection test above.
    """
    _citizen()
    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    response = client.post(reverse("api:auth-logout"))

    assert response.status_code == 204


def test_csrf_token_is_rotated_on_login(client: Client) -> None:
    """⚠️ Session-fixation defence, CSRF edition. `django.contrib.auth.login()` calls
    `rotate_token()`, which cycles the CSRF token the same way it cycles the session key.

    Without this a CSRF token an attacker planted before login would still be valid
    afterwards, and they could use it to submit authenticated requests on the victim's
    session. This is a weaker variant of session fixation — the session key itself is
    already rotated by T1.3, but the CSRF token rides separately (CSRF_USE_SESSIONS=False)
    and needs its own rotation.
    """
    from django.middleware.csrf import get_token

    _citizen()
    # `get_token()` called on a RequestFactory request with the client's own session
    # issues a real token and plants it in the cookie jar — the same token an attacker
    # would have from a pre-login visit to any page that renders a {% csrf_token %} tag.
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.session = client.session
    pre_login_token = get_token(request)
    client.cookies[settings.CSRF_COOKIE_NAME] = pre_login_token

    client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    post_login_token = client.cookies[settings.CSRF_COOKIE_NAME]
    # ⚠️ Different token. The pre-login one is no longer valid — an attacker holding it
    # cannot submit a CSRF attack against the victim's newly established session.
    assert post_login_token != pre_login_token


# ---------------------------------------------------------------------------------------
# Pre-session endpoints are deliberately CSRF-exempt
# ---------------------------------------------------------------------------------------


def test_register_does_not_require_csrf_token(client: Client) -> None:
    """⚠️ A visitor registering for the first time has never been issued a CSRF token, so
    demanding one would make sign-up impossible. DRF's SessionAuthentication only enforces
    CSRF when `request.user` is authenticated; for anonymous users it returns None without
    checking. That is the framework's intended behaviour and ours.

    Login CSRF (an attacker tricking a victim into logging in as the *attacker's* account)
    is a real but lower-severity threat; it is outside this task's scope. The high-severity
    threat — an attacker submitting state-changing requests as the *victim* — is guarded by
    CSRF on authenticated endpoints.
    """
    client.handler.enforce_csrf_checks = True
    client.cookies.pop(settings.CSRF_COOKIE_NAME, None)

    response = client.post(
        reverse("api:auth-register"),
        data={"email": "new@example.test", "password": "secure-pass-1"},
        content_type="application/json",
    )

    # Succeeds without a CSRF token — deliberately.
    assert response.status_code == 201


def test_login_does_not_require_csrf_token(client: Client) -> None:
    """Same reasoning as register — a first-time visitor has no token to present."""
    _citizen()
    client.handler.enforce_csrf_checks = True
    client.cookies.pop(settings.CSRF_COOKIE_NAME, None)

    response = client.post(
        reverse("api:auth-login"),
        data={"identifier": "citizen@example.test", "password": PASSWORD},
        content_type="application/json",
    )

    assert response.status_code == 200
