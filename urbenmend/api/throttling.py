"""
Rate limiting for auth endpoints (T1.8, FR-4, API §4.5).

⚠️ **The numbers are our policy, not spec-derived.** `api-conventions.md` lists "numeric rate
limits and windows" under "Not specified — do not invent". These follow T1.2's precedent for the
same tension (`CODE_LENGTH`/`TTL`/`MAX_ATTEMPTS`): pick defensible values, put them in settings so
tuning is config rather than code (NFR-11), and label them as chosen rather than mandated.

⚠️ **Lockout is throttle-only backoff** (T1.8 decision). Failed logins consume the bucket;
success clears it. No per-account lock state, so this cannot be weaponised — an attacker burning
an authority's identifier bucket delays that bucket, not the account, and the real owner still
authenticates from their own IP. That is the "backoff" half of FR-4's "lockout/backoff".

Two divergences from DRF, both verified against the installed source rather than assumed:

1. ⚠️ **`SimpleRateThrottle.parse_rate` reads only `period[0]`**, so `"5/15min"` silently means
   5-per-*minute* — the `15` is discarded. There is no multiplier syntax. `ScopedWindowRateThrottle`
   overrides it so a 15-minute window can actually be expressed.
2. ⚠️ **DRF emits only `Retry-After`, and only on 429.** API §4.5 requires `RateLimit-Limit`,
   `-Remaining` and `-Reset` on *every* limited endpoint. `RateLimitHeadersMixin` adds them.
   `check_throttles()` stores nothing on the request, so the mixin captures the throttle instances
   via `get_throttles()` and reads their post-`allow_request` state.

[doc: API §4.5, FR-4, workflow §722-723 "Test that the 429 fires and includes Retry-After"]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from rest_framework.throttling import SimpleRateThrottle

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.response import Response
    from rest_framework.views import APIView

# DRF's own unit letters (its docstring: 's', 'sec', 'm', 'min', 'h', 'hour', 'd', 'day'), kept
# identical so a plain `"10/hour"` still behaves exactly as it does everywhere else in DRF.
_PERIOD_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# ⚠️ Policy, not spec (see module docstring). Overridable per environment via `AUTH_THROTTLE_RATES`
# in settings; tests override the same key.
_DEFAULT_RATES = {
    "auth_anon": "10/15m",
    "auth_identity": "5/15m",
    "auth_user": "20/15m",
}


class ScopedWindowRateThrottle(SimpleRateThrottle):
    """`SimpleRateThrottle` with a window that can span more than one unit.

    ⚠️ Rates are read from `settings.AUTH_THROTTLE_RATES` at instantiation, not bound as a class
    attribute. DRF's `THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES` binds the dict once at
    import, so `override_settings` in a test would not reach it — and a rate limit that cannot be
    turned down in a test is a rate limit whose 429 path never gets exercised.
    """

    # Set by `SimpleRateThrottle.__init__` and `allow_request`, neither of which carries
    # annotations. Declared here so `headers()` type-checks against them rather than `Any`.
    num_requests: int | None
    duration: int | None
    history: list[float]
    now: float

    def get_rate(self) -> str:
        scope = getattr(self, "scope", None)
        if not scope:
            raise ImproperlyConfigured(f"{type(self).__name__} must set `.scope`.")
        rates: dict[str, str] = {**_DEFAULT_RATES, **getattr(settings, "AUTH_THROTTLE_RATES", {})}
        try:
            return rates[scope]
        except KeyError as exc:
            raise ImproperlyConfigured(f"No throttle rate configured for scope '{scope}'.") from exc

    def parse_rate(self, rate: str | None) -> tuple[int | None, int | None]:
        """Parse `"<count>/<n><unit>"` — e.g. `"5/15m"` is 5 requests per 900 seconds.

        ⚠️ The override exists because DRF's version does `_PERIOD_SECONDS[period[0]]` and throws
        the rest of the string away. `"5/15m"` there means 5 per *minute*: a 15× tighter limit than
        written, with nothing to indicate it. Windows below one unit are why FR-4 backoff needs
        this — a login limit is naturally "N per quarter hour", not "N per minute".
        """
        if rate is None:
            return (None, None)
        try:
            count_text, period = rate.split("/")
            num_requests = int(count_text)
        except ValueError as exc:
            raise ImproperlyConfigured(f"Malformed throttle rate '{rate}'.") from exc

        digits = "".join(ch for ch in period if ch.isdigit())
        unit = period.lstrip("0123456789")
        multiplier = int(digits) if digits else 1
        if not unit or unit[0] not in _PERIOD_SECONDS:
            raise ImproperlyConfigured(
                f"Unknown period '{period}' in throttle rate '{rate}'. "
                f"Expected one of {sorted(_PERIOD_SECONDS)} optionally prefixed by a count."
            )
        return (num_requests, multiplier * _PERIOD_SECONDS[unit[0]])

    def headers(self) -> tuple[int, int, int] | None:
        """`(limit, remaining, reset)` for API §4.5, or `None` if this throttle did not apply.

        ⚠️ Reads the state `allow_request()` leaves behind. A throttle that returned early — no
        rate, or `get_cache_key()` returned `None` — never sets `history`, so `hasattr` is the test
        for "did this bucket actually get consulted". Reporting zeros for a bucket that was never
        checked would advertise a limit the endpoint does not enforce.
        """
        if not hasattr(self, "history") or self.num_requests is None or self.duration is None:
            return None
        # After `throttle_success()` the current request is already in `history`, so this is exact
        # rather than an estimate. On the failure path `history` is full and remaining is 0.
        remaining = max(0, self.num_requests - len(self.history))
        # Capacity returns when the oldest request in the window ages out.
        oldest = self.history[-1] if self.history else self.now
        return (self.num_requests, remaining, int(oldest + self.duration))


class AuthAnonRateThrottle(ScopedWindowRateThrottle):
    """Per-IP bucket for pre-session auth endpoints (API §4.5 "and per-IP").

    ⚠️ Deliberately keyed on IP for *every* caller, authenticated or not — unlike DRF's
    `AnonRateThrottle`, which returns `None` (unlimited) as soon as a user is present. On these
    endpoints the point is to cap one source's attempts across *many* identifiers; exempting
    authenticated callers would leave a session-holder free to spray an identifier list.
    """

    scope = "auth_anon"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthIdentityRateThrottle(ScopedWindowRateThrottle):
    """Per-identifier bucket — the FR-4 brute-force limit, and the "lockout/backoff" half.

    ⚠️ Keys on the **submitted** `identifier`, not on `request.user`. At login there is no user
    yet, and that is the whole point: this is what caps guesses against one account. Keying on
    `request.user` would only ever throttle *after* a successful password check, i.e. never
    during the attack it exists to stop.

    ⚠️ The identifier is **hashed into the key**, never stored raw. Cache keys surface in
    `redis-cli KEYS`, slow logs and dumps; an email address there is PII (NFR-12) and a phone
    number is worse. Normalized first so `Citizen@Example.test` and `citizen@example.test` share
    one bucket — otherwise case alone multiplies the allowance.

    Returns `None` (unlimited) when the body carries no identifier, so a malformed request is
    rejected by the serializer as `400` rather than burning someone else's bucket.
    """

    scope = "auth_identity"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        # ⚠️ `request.data` parses the body, so it raises on malformed JSON or an unsupported
        # content type. Letting that escape would abort `check_throttles()` mid-list — the per-IP
        # bucket registered after this one would never count the request, so a flood of malformed
        # bodies would consume no budget at all. Returning `None` instead means "this bucket does
        # not apply"; the request still reaches the parser, which raises the same 400/415 it always
        # would, and `auth_anon` still counts it.
        try:
            data = request.data
        except Exception:  # noqa: BLE001 — any parse failure, deliberately not just ParseError.
            return None

        identifier = data.get("identifier") if hasattr(data, "get") else None
        if not identifier or not isinstance(identifier, str):
            return None
        from hashlib import sha256

        normalized = identifier.strip().lower()
        digest = sha256(normalized.encode("utf-8")).hexdigest()[:32]
        return self.cache_format % {"scope": self.scope, "ident": digest}


class AuthUserRateThrottle(ScopedWindowRateThrottle):
    """Per-identity bucket for auth endpoints reached *with* a session (API §4.5 "per-identity").

    Covers `/auth/2fa/verify` on a full session and `/auth/verify` for an additional channel.

    ⚠️ A partial post-password session is **not** authenticated (`request.user` is `AnonymousUser`
    by T1.7's design), so a 2FA-code attack lands in `auth_anon` rather than here. That is correct
    and load-bearing: the tighter per-IP bucket is the one guarding code guesses.
    """

    scope = "auth_user"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class RateLimitHeadersMixin:
    """Emit `RateLimit-Limit`/`-Remaining`/`-Reset` on every response (API §4.5).

    ⚠️ **DRF supplies none of these.** It sets `Retry-After`, only on a 429. §4.5 requires all
    three on every limited endpoint, so this is a required custom layer in the same family as
    T0.6's camelCase and pagination gaps — not a nicety.

    ⚠️ `check_throttles()` keeps its throttle instances local and stores nothing on the request,
    so there is no post-hoc state to read. `get_throttles()` is overridden to capture the exact
    instances DRF then calls `allow_request()` on; the mixin reads their state afterwards. This is
    why it is a mixin on the view rather than middleware.
    """

    def get_throttles(self) -> list[Any]:
        throttles: list[Any] = super().get_throttles()  # type: ignore[misc]
        self._throttle_instances = throttles
        return throttles

    def finalize_response(
        self, request: Request, response: Response, *args: Any, **kwargs: Any
    ) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)  # type: ignore[misc]

        reported = [
            headers
            for throttle in getattr(self, "_throttle_instances", [])
            if isinstance(throttle, ScopedWindowRateThrottle)
            and (headers := throttle.headers()) is not None
        ]
        if not reported:
            return response

        # ⚠️ The binding limit is the bucket with the least headroom, which is not necessarily the
        # smallest limit — a wide-but-nearly-spent bucket constrains the caller more than a narrow
        # fresh one. Advertising the wrong bucket tells a well-behaved client it has room it does
        # not have, which is exactly the client that would then trip a 429.
        limit, remaining, reset = min(reported, key=lambda h: h[1])
        response["RateLimit-Limit"] = str(limit)
        response["RateLimit-Remaining"] = str(remaining)
        response["RateLimit-Reset"] = str(reset)
        return response


def clear_identity_throttle(*, request: Request) -> None:
    """Forget the failed-attempt history for this request's identifier (T1.8, FR-4 backoff).

    ⚠️ **Success path only**, and only after the password has actually been verified. Calling it
    before authentication — or on the failure path — removes the counter that makes the endpoint
    rate-limited at all, and the tests would still pass because the happy path never notices.

    Clears `auth_identity` alone. The per-IP bucket deliberately survives a success: one source
    working through a credential dump gets a valid login every so often, and clearing on those
    would hand it an unlimited overall budget.
    """
    throttle = AuthIdentityRateThrottle()
    key = throttle.get_cache_key(request, view=None)  # type: ignore[arg-type]
    if key:
        cache.delete(key)
