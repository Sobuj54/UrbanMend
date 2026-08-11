"""
Rate limiting for auth (T1.8, FR-4) and submission (T2.9, FR-33) endpoints — API §4.5.

⚠️ **The numbers are our policy, not spec-derived.** `api-conventions.md` lists "numeric rate
limits and windows" under "Not specified — do not invent". These follow T1.2's precedent for the
same tension (`CODE_LENGTH`/`TTL`/`MAX_ATTEMPTS`): pick defensible values, put them in settings so
tuning is config rather than code (NFR-11), and label them as chosen rather than mandated.

⚠️ **Lockout is throttle-only backoff** (T1.8 decision). Failed logins consume the bucket;
success clears it. No per-account lock state, so this cannot be weaponised — an attacker burning
an authority's identifier bucket delays that bucket, not the account, and the real owner still
authenticates from their own IP. That is the "backoff" half of FR-4's "lockout/backoff".

⚠️ **Auth and submission rates live in two settings dicts, merged here by scope name.** They are
different policies with different shapes — five login attempts a quarter hour versus sixty photos an
hour — and one dict would invite an operator tightening spam controls into locking citizens out of
their accounts. The `auth_*` / `submit_*` scope prefixes are what keeps the merge unambiguous.

Two divergences from DRF, both verified against the installed source rather than assumed:

1. ⚠️ **`SimpleRateThrottle.parse_rate` reads only `period[0]`**, so `"5/15min"` silently means
   5-per-*minute* — the `15` is discarded. There is no multiplier syntax. `ScopedWindowRateThrottle`
   overrides it so a 15-minute window can actually be expressed.
2. ⚠️ **DRF emits only `Retry-After`, and only on 429.** API §4.5 requires `RateLimit-Limit`,
   `-Remaining` and `-Reset` on *every* limited endpoint. `RateLimitHeadersMixin` adds them.
   `check_throttles()` stores nothing on the request, so the mixin captures the throttle instances
   via `get_throttles()` and reads their post-`allow_request` state.

[doc: API §4.5, FR-4, FR-33, workflow §722-723 "Test that the 429 fires and includes Retry-After"]
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
# and `SUBMISSION_THROTTLE_RATES` in settings; tests override the same keys.
#
# ⚠️ **This is the fallback for every scope, not a mirror of either settings dict.** An
# `override_settings(AUTH_THROTTLE_RATES={"auth_anon": "1/1h"})` replaces the whole dict, so the
# scopes it omits resolve here — which is what keeps a test that tightens one bucket from
# accidentally disabling the others.
_DEFAULT_RATES = {
    "auth_anon": "10/15m",
    "auth_identity": "5/15m",
    "auth_user": "20/15m",
    "submit_report": "20/1h",
    "submit_media": "60/1h",
    "submit_ip": "120/1h",
}

# The settings dicts consulted, in order. Later entries win on a duplicate scope, which the
# `auth_*`/`submit_*` prefixes make impossible in practice.
_RATE_SETTINGS = ("AUTH_THROTTLE_RATES", "SUBMISSION_THROTTLE_RATES")


class ScopedWindowRateThrottle(SimpleRateThrottle):
    """`SimpleRateThrottle` with a window that can span more than one unit.

    ⚠️ Rates are read from settings at instantiation, not bound as a class attribute. DRF's
    `THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES` binds the dict once at import, so
    `override_settings` in a test would not reach it — and a rate limit that cannot be turned down
    in a test is a rate limit whose 429 path never gets exercised.
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
        rates: dict[str, str] = dict(_DEFAULT_RATES)
        for name in _RATE_SETTINGS:
            rates.update(getattr(settings, name, {}) or {})
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


class PerAccountScopedThrottle(ScopedWindowRateThrottle):
    """Base for any bucket keyed on the authenticated account. One definition of that cache key.

    ⚠️ **Unauthenticated callers get `None` (this bucket does not apply), NOT an IP fallback.** DRF's
    own `UserRateThrottle` falls back to the IP for anonymous requests, which would file anonymous
    traffic under a scope named for accounts — so `RateLimit-Limit` would then advertise a per-account
    allowance to a caller who has no account, and one IP's anonymous requests would share a bucket
    with nothing meaningful. Every endpoint using these is `IsAuthenticated`, and DRF's `initial()`
    runs `check_permissions()` *before* `check_throttles()`, so an anonymous request is already a
    `401` by the time this would be consulted (verified against the installed source).
    """

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class PerIPScopedThrottle(ScopedWindowRateThrottle):
    """Base for any bucket keyed on the request's source address.

    ⚠️ `get_ident()` is DRF's, which honours `NUM_PROXIES`/`X-Forwarded-For` — the address is only as
    trustworthy as the ingress in front of it, and a misconfigured proxy count makes every request
    look like it came from the load balancer (one shared bucket for the whole city). That is a
    deployment concern DevOps §8 owns, not something a throttle can defend against.
    """

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthAnonRateThrottle(PerIPScopedThrottle):
    """Per-IP bucket for pre-session auth endpoints (API §4.5 "and per-IP").

    ⚠️ Deliberately keyed on IP for *every* caller, authenticated or not — unlike DRF's
    `AnonRateThrottle`, which returns `None` (unlimited) as soon as a user is present. On these
    endpoints the point is to cap one source's attempts across *many* identifiers; exempting
    authenticated callers would leave a session-holder free to spray an identifier list.
    """

    scope = "auth_anon"


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


class AuthUserRateThrottle(PerAccountScopedThrottle):
    """Per-identity bucket for auth endpoints reached *with* a session (API §4.5 "per-identity").

    Covers `/auth/2fa/verify` on a full session and `/auth/verify` for an additional channel.

    ⚠️ A partial post-password session is **not** authenticated (`request.user` is `AnonymousUser`
    by T1.7's design), so a 2FA-code attack lands in `auth_anon` rather than here. That is correct
    and load-bearing: the tighter per-IP bucket is the one guarding code guesses.
    """

    scope = "auth_user"


# --------------------------------------------------------------------------------------
# Submission (T2.9, FR-33, API §4.5 "tighter buckets on … report submission")
# --------------------------------------------------------------------------------------
# ⚠️ **Three buckets, and the two per-account ones are separate on purpose.** A single
# `submit_user` scope shared by `POST /reports` and `POST /media` would make a five-photo report
# spend six units of one allowance, so the only way to size it would be for the photo-heavy case —
# which then leaves text-only spam six times the budget it should have. Separate scopes let each
# endpoint be sized for what it actually costs to serve.
#
# ⚠️ **None of these clears on success.** `clear_identity_throttle()` exists because a legitimate
# login proves the caller is not guessing; a legitimate report proves nothing of the sort — volume
# *is* the thing FR-33 limits, so a successful submission must consume its budget or the limit only
# applies to failures.


class SubmissionRateThrottle(PerAccountScopedThrottle):
    """Per-account bucket for `POST /reports` (FR-33, and the per-account LLM cost cap, NFR-13).

    ⚠️ **Not applied to `GET /reports`.** FR-33 is about submission; a read that costs one indexed
    query is not the abuse surface, and throttling it would break the map and an Authority queue
    under legitimately heavy use. `throttle_classes` alone cannot express that — it applies to every
    method on the view — so `ReportCollectionView.get_throttles()` is where the split is made.
    """

    scope = "submit_report"


class MediaUploadRateThrottle(PerAccountScopedThrottle):
    """Per-account bucket for `POST /media` (FR-33).

    ⚠️ **The more attractive target of the two, which is why it is not simply the report bucket
    times `MEDIA_MAX_PER_REPORT`.** Each upload costs a decode, a re-encode, a storage write and a
    worker job; a report costs one INSERT. Sizing this to never bind before the report bucket would
    make the cheap endpoint the limit and leave the expensive one unbounded in practice — see the
    reasoning on `SUBMISSION_THROTTLE_RATES` in `settings/base.py`.
    """

    scope = "submit_media"


class SubmissionIPRateThrottle(PerIPScopedThrottle):
    """Per-IP bucket **shared by both submission endpoints** — the Sybil limit (PRD §T3, FR-33).

    ⚠️ **Shared deliberately, and it is the only bucket here that is.** The per-account buckets
    cannot see the attack PRD §T3 names: a farm of fresh accounts gets a fresh per-account allowance
    with every registration, and FR-1 verification raises the cost of each account without bounding
    how many an attacker makes. The address is the thing that does not rotate for free. Counting
    reports and uploads together is what makes it a limit on *submission traffic from one source*
    rather than two limits neither of which sees the total.

    ⚠️ **It never sees anonymous traffic**, because `check_permissions()` runs first on both
    endpoints (see `PerAccountScopedThrottle`). A `401` costs no rows, no bytes and no LLM calls, so
    there is nothing here for a per-IP counter to protect.
    """

    scope = "submit_ip"


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
