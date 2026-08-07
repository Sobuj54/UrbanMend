"""
T1.8 — login/OTP rate limiting and lockout backoff (FR-4, API §4.5).

⚠️ **Throttle state is cleared between tests by the autouse fixture in the root `conftest.py`**,
not here. It lives in the Redis `default` cache, which `pytest-django`'s transaction rollback does
not touch — without that fixture, buckets accumulate across the whole session and unrelated tests
fail with spurious `429`s in an order-dependent way. That conftest was added by this task.

The tests that matter most are `test_parse_rate_*` and `test_rate_limit_headers_*`: both cover
places where the obvious implementation is silently wrong rather than broken. A suite that only
asserted "the 429 fires" would pass against a 15×-too-tight window and against no `RateLimit-*`
headers at all.

Layered per `testing.md`: throttle-class unit tests own the parsing and key rules, view tests own
the HTTP contract.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from urbenmend.api import throttling
from urbenmend.identity.models import Role, User, UserStatus

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-1"
EMAIL = "citizen@example.test"


def _user(**overrides: object) -> User:
    fields: dict[str, object] = {
        "email": EMAIL,
        "password": PASSWORD,
        "status": UserStatus.ACTIVE,
        "role": Role.CITIZEN,
    }
    fields.update(overrides)
    return User.objects.create_user(**fields)  # type: ignore[arg-type]


def _login(client: Client, *, identifier: str = EMAIL, password: str = PASSWORD) -> object:
    return client.post(
        reverse("api:auth-login"),
        data={"identifier": identifier, "password": password},
        content_type="application/json",
    )


def _request(data: dict[str, str] | None = None) -> Request:
    """A DRF `Request` — what the throttle sees in production.

    ⚠️ `APIRequestFactory` returns a bare `WSGIRequest`, which has no `.data`; DRF only wraps it
    into a `Request` during dispatch. The throttles read `request.data`, so the unit tests must
    hand them the same object the view would — including the `parsers`, which `Request` does not
    default to and without which `.data` raises `UnsupportedMediaType`.
    """
    raw = APIRequestFactory().post("/", data or {}, format="json")
    return Request(raw, parsers=[JSONParser()])


# ---------------------------------------------------------------------------------------
# parse_rate — the multi-unit window DRF cannot express
# ---------------------------------------------------------------------------------------


def test_parse_rate_honours_a_multi_unit_window() -> None:
    """⚠️ The regression guard for the whole module.

    DRF's `parse_rate` does `{'s':1,'m':60,...}[period[0]]` and discards the rest of the string, so
    "5/15m" means 5-per-*minute* there — a 15× tighter limit than written, with nothing anywhere to
    indicate it. Every rate in `AUTH_THROTTLE_RATES` uses this syntax.
    """
    throttle = throttling.AuthIdentityRateThrottle()

    assert throttle.parse_rate("5/15m") == (5, 900)


def test_parse_rate_still_matches_drf_for_a_bare_unit() -> None:
    """A plain "10/hour" must behave exactly as it does everywhere else in DRF."""
    throttle = throttling.AuthIdentityRateThrottle()

    assert throttle.parse_rate("10/hour") == (10, 3600)
    assert throttle.parse_rate("3/s") == (3, 1)
    assert throttle.parse_rate("100/d") == (100, 86400)


def test_parse_rate_rejects_an_unknown_period() -> None:
    """A typo must fail loudly at startup, not silently pick a wrong window."""
    throttle = throttling.AuthIdentityRateThrottle()

    with pytest.raises(ImproperlyConfigured):
        throttle.parse_rate("5/15weeks")


def test_an_unconfigured_scope_raises_rather_than_defaulting() -> None:
    """⚠️ Fail closed on misconfiguration — a silent fallback would mean "unlimited"."""

    class Unconfigured(throttling.ScopedWindowRateThrottle):
        scope = "nonexistent_scope"

    with pytest.raises(ImproperlyConfigured):
        Unconfigured()


# ---------------------------------------------------------------------------------------
# Cache keys — what each bucket is actually keyed on
# ---------------------------------------------------------------------------------------


def test_the_identifier_bucket_never_stores_the_raw_identifier() -> None:
    """⚠️ Cache keys leak: `redis-cli KEYS *`, slow logs, dumps. An email there is PII (NFR-12).

    A phone number is worse — it is a direct contact channel for every user who registered by SMS.
    """
    key = throttling.AuthIdentityRateThrottle().get_cache_key(
        _request({"identifier": EMAIL}),
        view=None,  # type: ignore[arg-type]
    )

    assert key is not None
    assert EMAIL not in key
    assert "citizen" not in key


def test_the_identifier_bucket_is_case_and_whitespace_insensitive() -> None:
    """Otherwise typing `Citizen@Example.test` hands the attacker a fresh allowance per casing."""
    throttle = throttling.AuthIdentityRateThrottle()
    lower = throttle.get_cache_key(_request({"identifier": EMAIL}), view=None)  # type: ignore[arg-type]
    messy = throttle.get_cache_key(_request({"identifier": f"  {EMAIL.upper()}  "}), view=None)  # type: ignore[arg-type]

    assert lower == messy


def test_a_missing_identifier_is_not_throttled_by_the_identity_bucket() -> None:
    """A malformed body should reach the serializer's `400`, not burn an unrelated bucket."""
    assert throttling.AuthIdentityRateThrottle().get_cache_key(_request(), view=None) is None  # type: ignore[arg-type]


def test_the_ip_bucket_applies_to_authenticated_callers_too() -> None:
    """⚠️ Unlike DRF's `AnonRateThrottle`, which exempts them.

    On auth endpoints the per-IP cap exists to stop one source spraying *many* identifiers, so
    exempting session-holders would leave a logged-in attacker unlimited.
    """
    request = _request()
    request.user = _user()

    assert throttling.AuthAnonRateThrottle().get_cache_key(request, view=None) is not None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------
# The 429 — FR-4's brute-force limit over HTTP
# ---------------------------------------------------------------------------------------

# ⚠️ Rates are tightened here rather than looping 10+ times at the real values: the point is the
# behaviour at the boundary, and a test that spends the production allowance is slow and tells you
# nothing extra. `AUTH_THROTTLE_RATES` is read at throttle *instantiation*, which is what makes
# `override_settings` reach it — DRF's own `THROTTLE_RATES` binds at import and would ignore this.
_TIGHT = {"auth_anon": "3/15m", "auth_identity": "2/15m", "auth_user": "2/15m"}


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_repeated_failed_logins_are_rate_limited(client: Client) -> None:
    """The workflow's stated T1.8 requirement: the `429` fires (workflow §722-723)."""
    _user()

    first = _login(client, password="wrong-1")
    second = _login(client, password="wrong-2")
    third = _login(client, password="wrong-3")

    assert first.status_code == 401  # type: ignore[attr-defined]
    assert second.status_code == 401  # type: ignore[attr-defined]
    # `auth_identity` is 2/15m, so the third attempt is refused before the password is checked.
    assert third.status_code == 429  # type: ignore[attr-defined]


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_the_429_includes_retry_after(client: Client) -> None:
    """The other half of the workflow's requirement, and the only header DRF supplies itself."""
    _user()
    _login(client, password="wrong-1")
    _login(client, password="wrong-2")

    throttled = _login(client, password="wrong-3")

    assert throttled.status_code == 429  # type: ignore[attr-defined]
    assert "Retry-After" in throttled  # type: ignore[operator]
    assert int(throttled["Retry-After"]) > 0  # type: ignore[index]


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_the_429_uses_the_spec_error_envelope(client: Client) -> None:
    """API §4.2 — `RATE_LIMITED`, rendered by the T0.6 handler like every other error."""
    _user()
    _login(client, password="wrong-1")
    _login(client, password="wrong-2")

    body = _login(client, password="wrong-3").json()  # type: ignore[attr-defined]

    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["traceId"]


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_a_throttled_login_does_not_authenticate_even_with_the_right_password(
    client: Client,
) -> None:
    """⚠️ The throttle must run *before* the credential check, not alongside it.

    If a correct password could still pass while throttled, the limit would cap only the attacker's
    *reporting* of failure, not their attempts — the run that finds the password is exactly the one
    that must be refused.
    """
    from django.contrib.auth import SESSION_KEY

    _user()
    _login(client, password="wrong-1")
    _login(client, password="wrong-2")

    response = _login(client)  # correct password

    assert response.status_code == 429  # type: ignore[attr-defined]
    assert SESSION_KEY not in client.session


# ---------------------------------------------------------------------------------------
# Backoff, not lockout — the T1.8 mechanism decision
# ---------------------------------------------------------------------------------------


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_a_successful_login_clears_the_identifier_backoff(client: Client) -> None:
    """FR-4 backoff must not punish a legitimate user who mistyped once.

    ⚠️ This is what `clear_identity_throttle()` buys, and the test that fails if the call is moved
    off the success path.
    """
    _user()
    assert _login(client, password="wrong-1").status_code == 401  # type: ignore[attr-defined]
    assert _login(client).status_code == 200  # type: ignore[attr-defined]

    client.logout()

    # Without the clear, the earlier failure would still be in the window and this would be a 429.
    assert _login(client, password="wrong-2").status_code == 401  # type: ignore[attr-defined]


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_a_locked_out_identifier_does_not_lock_the_account_itself(client: Client) -> None:
    """⚠️ The reason throttle-only backoff was chosen over per-account lockout state (T1.8).

    An attacker who knows an Authority's email must not be able to hold them out. Burning the
    identifier bucket from one client leaves a different client able to authenticate as that same
    user — which a `locked_until` column on `User` would not.
    """
    user = _user()
    for password in ("wrong-1", "wrong-2", "wrong-3"):
        _login(client, password=password)
    assert _login(client).status_code == 429  # type: ignore[attr-defined]

    # A different client: fresh per-IP bucket, and the identifier bucket is not account state.
    # Clearing only the identity bucket simulates the attacker's window expiring while proving the
    # account itself was never marked locked.
    #
    # ⚠️ Django's test `Client` defaults every request to 127.0.0.1, so "another client" must also
    # say `REMOTE_ADDR` — two clients on the same IP share the `auth_anon` bucket by design.
    throttling.clear_identity_throttle(request=_request({"identifier": EMAIL}))
    victim = Client()
    response = victim.post(
        reverse("api:auth-login"),
        data={"identifier": EMAIL, "password": PASSWORD},
        content_type="application/json",
        REMOTE_ADDR="10.0.0.9",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.status == UserStatus.ACTIVE  # never touched


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_the_ip_bucket_survives_a_successful_login(client: Client) -> None:
    """⚠️ Success clears the identifier bucket only.

    One source working a credential dump gets a valid login every so often; clearing the per-IP
    bucket on those would hand it an unlimited overall budget.
    """
    _user()
    _user(email="other@example.test")
    _login(client, identifier="other@example.test", password="wrong-1")  # 1 of 3 on auth_anon
    client.logout()
    assert _login(client).status_code == 200  # type: ignore[attr-defined]  # 2 of 3
    client.logout()

    # 3 of 3 consumed, so the next distinct identifier trips the per-IP cap despite the success.
    assert _login(client, identifier="third@example.test").status_code in (401, 429)  # type: ignore[attr-defined]
    assert _login(client, identifier="fourth@example.test").status_code == 429  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------
# RateLimit-* headers — API §4.5, on every limited endpoint
# ---------------------------------------------------------------------------------------


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_rate_limit_headers_are_present_on_a_successful_response(client: Client) -> None:
    """⚠️ API §4.5 requires these on **every** limited endpoint, not only on a 429.

    DRF ships none of them and `check_throttles()` stores nothing on the request, so the obvious
    implementation emits no headers at all and nothing else in the system notices. This is the
    assertion that catches that.
    """
    _user()

    response = _login(client)

    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response["RateLimit-Limit"] == "2"  # type: ignore[index]  # auth_identity binds
    assert response["RateLimit-Remaining"] == "1"  # type: ignore[index]
    assert int(response["RateLimit-Reset"]) > 0  # type: ignore[index]


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_rate_limit_headers_report_the_bucket_with_least_headroom(client: Client) -> None:
    """⚠️ Not the smallest limit — the one closest to being spent.

    A wide-but-nearly-exhausted bucket constrains the caller more than a narrow fresh one.
    Advertising the wrong one tells a well-behaved client it has room it does not have.
    """
    _user()
    _login(client, password="wrong-1")

    response = _login(client, password="wrong-2")

    # auth_identity: 2/15m, both now spent → 0 remaining. auth_anon: 3/15m, 1 left.
    assert response["RateLimit-Remaining"] == "0"  # type: ignore[index]
    assert response["RateLimit-Limit"] == "2"  # type: ignore[index]


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_rate_limit_headers_are_present_on_the_429(client: Client) -> None:
    _user()
    _login(client, password="wrong-1")
    _login(client, password="wrong-2")

    throttled = _login(client, password="wrong-3")

    assert throttled.status_code == 429  # type: ignore[attr-defined]
    assert throttled["RateLimit-Remaining"] == "0"  # type: ignore[index]
    # ⚠️ Both header families coexist: the T0.6 handler rebuilds the body and would drop these if
    # it replaced the response rather than mutating it.
    assert "Retry-After" in throttled  # type: ignore[operator]


def test_an_unthrottled_endpoint_advertises_no_rate_limit_headers(client: Client) -> None:
    """⚠️ Reporting a limit an endpoint does not enforce is worse than reporting none.

    `/health` carries no throttle classes, so the mixin must stay silent rather than emit zeros.
    """
    response = client.get(reverse("api:health"))

    assert "RateLimit-Limit" not in response


# ---------------------------------------------------------------------------------------
# The OTP half — /auth/2fa/verify and /auth/register
# ---------------------------------------------------------------------------------------


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_otp_verification_is_rate_limited(client: Client) -> None:
    """⚠️ A six-digit TOTP code is brute-forceable if the endpoint is not capped.

    `verify_token()` blocks *replay* of a code, not a walk through the keyspace, and unlike
    `verify_code()` (T1.2) there is no per-account attempt counter here. These buckets are the
    only cap that exists.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    user = _user(require_two_factor=True)
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    _login(client, password="wrong-1")  # 1 of 3 on auth_anon
    _login(client)  # 2 of 3 — partial session, so still anonymous

    url = reverse("api:auth-2fa-verify")
    body = {"code": "000000"}
    third = client.post(url, data=body, content_type="application/json")
    fourth = client.post(url, data=body, content_type="application/json")

    assert third.status_code == 422  # wrong code, 3 of 3
    assert fourth.status_code == 429


@override_settings(AUTH_THROTTLE_RATES=_TIGHT)
def test_registration_is_rate_limited(client: Client) -> None:
    """Caps automated account creation from one source (FR-33's spam concern, API §4.5)."""
    url = reverse("api:auth-register")

    statuses = [
        client.post(
            url,
            data={"email": f"new{index}@example.test", "password": PASSWORD},
            content_type="application/json",
        ).status_code
        for index in range(4)
    ]

    assert statuses[:3] == [201, 201, 201]
    assert statuses[3] == 429
