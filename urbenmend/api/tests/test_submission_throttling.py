"""T2.9 — submission rate limiting (FR-33, API §4.5).

The three buckets are `submit_report` and `submit_media` (per account) and `submit_ip` (per source,
**shared by both endpoints**). Four claims here are ones a working-looking implementation gets wrong
in silence:

1. **`GET /reports` must stay unthrottled.** `throttle_classes` applies to every method on a view, so
   the obvious wiring limits the read too — and the symptom is not an error, it is a map that stops
   loading for the busiest operator in the city.
2. **The rates must come from `SUBMISSION_THROTTLE_RATES`, and the auth rates must still resolve.**
   `get_rate()` merges two settings dicts; a version reading only one works perfectly until someone
   tunes the other.
3. **The per-IP bucket is shared.** Two buckets that happen to have the same rate look identical
   until you spend one from the other endpoint.
4. **A rejected request still consumes its bucket.** Exempting failures is the natural-looking
   "fix" and it makes the limit apply only to legitimate use.

⚠️ **This suite lives in `api/tests/` rather than in `reporting/` or `media/`**, because the shared
per-IP bucket is a rule about the two endpoints *together* — asserted from one app's suite it would
only ever be half-tested. The throttle classes themselves live in `urbenmend/api/throttling.py`.

⚠️ **Throttle state is cleared between tests by the autouse fixture in the root `conftest.py`.** It
lives in the Redis `default` cache, which `pytest-django`'s transaction rollback does not touch —
without it these buckets would leak into every later test in the session.

[doc: API §4.5 (amended 2026-08-11), §6.3, §6.4; FR-33, NFR-13, PRD §T3 Sybil resistance]
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from urbenmend.api import throttling
from urbenmend.geo.tests.factories import OUTSIDE_POINT
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.media.tests.factories import ReadyMediaFactory, image_bytes
from urbenmend.reporting.tests.factories import DEFAULT_LOCATION

pytestmark = pytest.mark.django_db

# Both patched at their **use** sites, the T2.2 rule: the services hold module-level references, so
# patching the task modules would leave those bindings pointing at the real Celery client.
REPORT_DELAY = "urbenmend.reporting.services.classify_report.delay"
MEDIA_DELAY = "urbenmend.media.services.process_media.delay"


def _rates(**overrides: str) -> dict[str, str]:
    """One bucket tightened, the rest set out of reach.

    ⚠️ **The unreachable values are the point, not padding.** Every `Client()` in this suite reports
    `127.0.0.1`, so they all share one `submit_ip` bucket — a test that tightened `submit_report` and
    left `submit_ip` at its production default could pass by tripping the wrong bucket, and would
    keep passing if the per-account bucket were deleted outright.
    """
    rates = {"submit_report": "1000/1h", "submit_media": "1000/1h", "submit_ip": "1000/1h"}
    rates.update(overrides)
    return rates


def _citizen_client(**overrides: Any) -> tuple[Client, User]:
    citizen = UserFactory.create(**overrides)
    client = Client()
    client.force_login(citizen)
    return client, citizen


def _submit(client: Client, **overrides: Any) -> Any:
    """`POST /reports` with a minimally valid §6.3 body — central Dhaka, inside the boundary."""
    body: dict[str, Any] = {
        "description": "Large pothole across the lane, two wheels deep.",
        "location": {"lng": DEFAULT_LOCATION.x, "lat": DEFAULT_LOCATION.y},
    }
    body.update(overrides)
    with patch(REPORT_DELAY):
        return client.post(reverse("api:reports"), body, content_type="application/json")


def _upload(client: Client) -> Any:
    """`POST /media` with a real decodable JPEG."""
    photo = SimpleUploadedFile("photo.jpg", image_bytes(), content_type="image/jpeg")
    with patch(MEDIA_DELAY):
        return client.post(reverse("api:media"), {"file": photo})


# ---------------------------------------------------------------------------------------
# The throttle classes — where the rates come from, and what the key is
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("throttle", "expected"),
    [
        (throttling.SubmissionRateThrottle, "submit_report"),
        (throttling.MediaUploadRateThrottle, "submit_media"),
        (throttling.SubmissionIPRateThrottle, "submit_ip"),
    ],
)
def test_every_submission_scope_resolves_to_a_configured_rate(
    throttle: type[throttling.ScopedWindowRateThrottle], expected: str
) -> None:
    """⚠️ A scope with no rate raises `ImproperlyConfigured` at *instantiation* — i.e. on the first
    request to the endpoint, in production, as a `500`. This is the cheap guard that a typo between
    the class and the settings dict cannot ship."""
    instance = throttle()

    assert instance.scope == expected
    assert instance.rate  # would have raised in `__init__` otherwise
    assert instance.num_requests and instance.duration == 3600


@override_settings(SUBMISSION_THROTTLE_RATES={"submit_report": "7/1h"})
def test_the_submission_rate_is_read_from_its_own_settings_dict() -> None:
    """⚠️ **Not from `AUTH_THROTTLE_RATES`.** The auth buckets are sized for credential guessing —
    five attempts a quarter hour — and borrowing them would cut a citizen off after their fifth
    photo. Read at instantiation, not bound as a class attribute, so this override reaches it."""
    assert throttling.SubmissionRateThrottle().num_requests == 7


@override_settings(SUBMISSION_THROTTLE_RATES={"submit_report": "7/1h"})
def test_tightening_submission_rates_leaves_the_auth_buckets_alone() -> None:
    """⚠️ **The merge, asserted in the direction that breaks silently.**

    `get_rate()` consults two settings dicts. A version that read only `SUBMISSION_THROTTLE_RATES`
    would raise here; a version that read only `AUTH_THROTTLE_RATES` would pass the test above by
    falling through to the module defaults and then ignore the operator's tuning forever.
    """
    assert throttling.AuthIdentityRateThrottle().num_requests == 5


@override_settings(AUTH_THROTTLE_RATES={"auth_identity": "2/15m"})
def test_tightening_auth_rates_leaves_the_submission_buckets_alone() -> None:
    """The same merge from the other side — an ops change to login backoff must not silently
    reconfigure spam control."""
    assert throttling.SubmissionRateThrottle().num_requests == 20


def test_an_unknown_scope_is_refused_rather_than_treated_as_unlimited() -> None:
    """⚠️ Failing closed on a misconfiguration. `SimpleRateThrottle` treats a `None` rate as
    unlimited, so a silent fallback would turn a typo into an endpoint with no limit at all."""

    class Misconfigured(throttling.PerAccountScopedThrottle):
        scope = "submit_nothing"

    with pytest.raises(ImproperlyConfigured):
        Misconfigured()


def test_a_per_account_bucket_does_not_apply_to_an_anonymous_caller() -> None:
    """⚠️ **`None`, not DRF's IP fallback.**

    `UserRateThrottle` keys anonymous requests on the IP, which would file them under a scope named
    for accounts and make `RateLimit-Limit` advertise a per-account allowance to a caller with no
    account. It costs nothing to omit: `check_permissions()` runs before `check_throttles()`, so
    every endpoint using these has already answered `401`.
    ⚠️ A DRF `Request`, not `APIRequestFactory`'s bare `WSGIRequest`: `.user` is a DRF property that
    only exists after the request has been wrapped, and `authenticators=()` is what makes it resolve
    to `AnonymousUser` — the same object the view hands the throttle. The T1.8 suite's `_request()`
    helper records the same trap for `request.data`.
    """
    raw = APIRequestFactory().post("/", {}, format="json")
    request = Request(raw, authenticators=())

    key = throttling.SubmissionRateThrottle().get_cache_key(request, view=None)  # type: ignore[arg-type]

    assert key is None


# ---------------------------------------------------------------------------------------
# POST /reports — and the GET that must not be limited with it
# ---------------------------------------------------------------------------------------


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="1/1h"))
def test_a_second_submission_past_the_account_limit_is_429() -> None:
    """FR-33. The envelope and `Retry-After` are §4.1/§4.5, rendered by the T0.6 handler."""
    client, _ = _citizen_client()

    assert _submit(client).status_code == 202
    throttled = _submit(client)

    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "RATE_LIMITED"
    assert throttled.json()["error"]["traceId"]
    assert int(throttled["Retry-After"]) > 0


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="1/1h"))
def test_reading_the_collection_is_not_limited_by_the_submission_bucket() -> None:
    """⚠️ **The deliverable of the `get_throttles()` split.**

    `throttle_classes` applies to every method on a view, so the obvious wiring throttles this read
    too. Nothing errors when that happens — the map and the Authority queue simply start returning
    `429` to whoever is using the product most, which is the opposite of what FR-33 wants.
    """
    client, _ = _citizen_client()
    _submit(client)
    assert _submit(client).status_code == 429

    for _ in range(4):
        assert client.get(reverse("api:reports")).status_code == 200


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="2/1h"))
def test_a_successful_submission_advertises_the_binding_bucket() -> None:
    """§4.5 requires all three headers on every limited endpoint; DRF supplies none of them.

    ⚠️ The advertised bucket is the one with least **headroom**, not the smallest limit — here the
    per-account bucket, because the per-IP one is nowhere near spent.
    """
    client, _ = _citizen_client()

    response = _submit(client)

    assert response["RateLimit-Limit"] == "2"
    assert response["RateLimit-Remaining"] == "1"
    assert int(response["RateLimit-Reset"]) > 0


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="1/1h"))
def test_the_collection_read_advertises_no_rate_limit_headers() -> None:
    """⚠️ Reporting a limit an endpoint does not enforce is worse than reporting none — a client
    would back off from a read it is free to make. The mixin stays silent because `get_throttles()`
    returns `[]` for `GET`, so no instance is ever captured."""
    client, _ = _citizen_client()

    response = client.get(reverse("api:reports"))

    assert "RateLimit-Limit" not in response


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="1/1h"))
def test_the_account_bucket_is_per_account() -> None:
    """One citizen exhausting their allowance must not silence their neighbour — the failure mode of
    keying on anything coarser (the IP alone, or a global counter)."""
    first, _ = _citizen_client()
    second, _ = _citizen_client()

    _submit(first)
    assert _submit(first).status_code == 429

    assert _submit(second).status_code == 202


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="1/1h"))
def test_a_rejected_submission_still_consumes_its_budget() -> None:
    """⚠️ **The natural-looking "fix" is to refund a failed request, and it is wrong.**

    DRF consumes the bucket in `allow_request()`, before the handler runs. Serving invalid content is
    not cheaper than serving valid content — an out-of-city point still costs a parse, a boundary
    query and a log line — so refunding failures would leave the limit applying only to legitimate
    use, which is precisely backwards for an anti-spam control.
    """
    client, _ = _citizen_client()

    rejected = _submit(client, location={"lng": OUTSIDE_POINT.x, "lat": OUTSIDE_POINT.y})
    assert rejected.status_code == 422

    assert _submit(client).status_code == 429


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_report="2/1h"))
def test_an_idempotent_replay_consumes_its_budget(
    django_capture_on_commit_callbacks: Any,
) -> None:
    """⚠️ Documented in §4.5 rather than exempted. Exempting a replay would mean doing the §4.6
    lookup inside a throttle's `get_cache_key()` — moving idempotency into the throttle layer to save
    a client that is already far below the limit. `RateLimit-Remaining` is the signal instead.

    ⚠️ **The commit callbacks must be executed for the first request, or this tests the wrong
    thing.** `complete()` runs from `transaction.on_commit` (T2.3), and `pytest-django` never commits
    — so without the capture the record is still *in progress* and the second call answers
    `409 IDEMPOTENCY_IN_PROGRESS`, which is a different code path that happens to also spend budget.
    """
    client, _ = _citizen_client()
    url = reverse("api:reports")
    body = {
        "description": "Large pothole across the lane, two wheels deep.",
        "location": {"lng": DEFAULT_LOCATION.x, "lat": DEFAULT_LOCATION.y},
    }

    with patch(REPORT_DELAY), django_capture_on_commit_callbacks(execute=True):
        first = client.post(url, body, content_type="application/json", HTTP_IDEMPOTENCY_KEY="k-1")
    with patch(REPORT_DELAY):
        replay = client.post(url, body, content_type="application/json", HTTP_IDEMPOTENCY_KEY="k-1")

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay["Idempotency-Replayed"] == "true"
    assert _submit(client).status_code == 429


# ---------------------------------------------------------------------------------------
# POST /media — the expensive endpoint, and its own bucket
# ---------------------------------------------------------------------------------------


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_media="1/1h"))
def test_a_second_upload_past_the_account_limit_is_429() -> None:
    """FR-33 covers the upload route too — §6.4 was ambiguous about it and §4.5 now names it."""
    client, _ = _citizen_client()

    assert _upload(client).status_code == 202
    throttled = _upload(client)

    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "RATE_LIMITED"


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_media="1/1h"))
def test_exhausting_the_upload_bucket_does_not_block_a_text_only_report() -> None:
    """⚠️ **Two per-account scopes, not one shared `submit_user`.**

    A single shared per-account bucket would make a five-photo report spend six units, so the only
    way to size it would be for the photo-heavy case — which then hands text-only spam six times the
    budget it should have. The endpoints cost different amounts to serve and are limited separately.
    """
    client, _ = _citizen_client()
    _upload(client)
    assert _upload(client).status_code == 429

    assert _submit(client).status_code == 202


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_ip="2/1h"))
def test_the_per_ip_bucket_is_shared_across_both_submission_endpoints() -> None:
    """⚠️ **The highest-value assertion in this module.**

    Two buckets that happen to carry the same rate look identical until one is spent from the other
    endpoint. Here two uploads exhaust the per-source allowance and the *report* endpoint is the one
    that answers `429` — which is what makes a five-photo report cost six units of one limit rather
    than one unit of two independent ones. §4.5 documents the consequence so a client can explain it.
    """
    client, _ = _citizen_client()

    assert _upload(client).status_code == 202
    assert _upload(client).status_code == 202

    assert _submit(client).status_code == 429


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_ip="2/1h"))
def test_the_per_ip_bucket_binds_across_separate_accounts() -> None:
    """⚠️ **The Sybil control (PRD §T3), and the reason a per-account bucket is not enough.**

    A farm of fresh accounts gets a fresh per-account allowance with every registration — FR-1
    verification raises the cost of each account without bounding how many an attacker makes. The
    source address is the thing that does not rotate for free.
    """
    for _ in range(2):
        client, _ = _citizen_client()
        assert _submit(client).status_code == 202

    third, _ = _citizen_client()
    assert _submit(third).status_code == 429


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_ip="1/1h"))
def test_an_anonymous_submission_costs_no_budget() -> None:
    """⚠️ **`check_permissions()` runs before `check_throttles()`** (verified against DRF's installed
    `APIView.initial`), so a `401` never reaches a counter.

    That ordering is what makes it safe for the per-account buckets to return `None` for anonymous
    callers — and it is correct on its own terms: a `401` writes no rows, stores no bytes and calls
    no LLM, so there is nothing for a per-IP bucket to protect. If the ordering ever reversed, an
    anonymous flood would silently spend the whole neighbourhood's submission allowance.
    """
    anonymous = Client()
    for _ in range(3):
        assert _submit(anonymous).status_code == 401

    client, _ = _citizen_client()
    assert _submit(client).status_code == 202


# ---------------------------------------------------------------------------------------
# What stays unlimited
# ---------------------------------------------------------------------------------------


@override_settings(SUBMISSION_THROTTLE_RATES=_rates(submit_media="1/1h", submit_ip="1/1h"))
def test_reading_a_photo_is_not_limited_by_the_upload_buckets() -> None:
    """`GET /media/{id}` is public (§6.4, following Q7) and carries no throttle classes at all.

    ⚠️ The reads live on `MediaDetailView` rather than beside the upload precisely so they cannot
    inherit `MediaUploadView`'s buckets by accident — the reason `MediaUploadView` needs no
    `get_throttles()` split of its own.
    """
    media = ReadyMediaFactory.create()
    client, _ = _citizen_client()
    _upload(client)

    url = reverse("api:media-detail", kwargs={"media_id": str(media.pk)})
    for _ in range(3):
        response = client.get(url)
        assert response.status_code == 200
    assert "RateLimit-Limit" not in response
