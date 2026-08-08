"""
`api/idempotency.py` — the store's own guarantees (T2.3, BR-5, API §4.6).

`reporting/tests/test_submission.py` covers the endpoint contract. This file covers the mechanism
underneath it, and in particular the two properties an endpoint test cannot reach:

  - **the concurrency claim** (R-2's mandated mitigation, `docs/08-coding-workflow.md`: "write a test
    that fires two concurrent requests and asserts no duplicate is created"). `cache.add()` is the
    whole basis of that claim, and a sequential test cannot distinguish it from a `get()`/`set()`
    pair that would let both callers through;
  - **what does and does not reach Redis** — no raw key, no request content (NFR-12).

⚠️ **The Redis `default` cache is real here, not a locmem stand-in.** `cache.add()`'s atomicity is a
property of the backend (`SETNX`), so overriding `CACHES` to locmem for these tests would assert a
guarantee the deployed system does not get from the same code path. The root `conftest.py` clears the
cache around every test, which is what keeps keys from leaking between them.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import override_settings

from urbenmend.api import idempotency
from urbenmend.api.exceptions import IdempotencyInProgress, IdempotencyKeyReused

SCOPE = "tests.create"
USER = "5f9c1f3a-0000-4000-8000-000000000001"
KEY = "3f2a6f3e-1111-4000-8000-000000000000"
PRINT = "fingerprint-a"
OTHER_PRINT = "fingerprint-b"


def _reserve(**overrides: Any) -> idempotency.Reservation | idempotency.Replay:
    kwargs: dict[str, Any] = {
        "scope": SCOPE,
        "user_id": USER,
        "key": KEY,
        "request_fingerprint": PRINT,
    }
    kwargs.update(overrides)
    return idempotency.reserve(**kwargs)


# ---------------------------------------------------------------------------------------
# normalize_key — §4.6's "the header is optional", and the length bound
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_absent_and_blank_headers_normalize_to_none(raw: str | None) -> None:
    """⚠️ `""` must not become a key.

    Some HTTP clients send an empty header for an unset value. Treating that as a real key would put
    every such client in one bucket, so the second caller would be handed the first one's response.
    """
    assert idempotency.normalize_key(raw) is None


def test_a_key_is_stripped_not_rejected() -> None:
    """Surrounding whitespace is transport noise, not part of the client's identifier."""
    assert idempotency.normalize_key(f"  {KEY}  ") == KEY


@override_settings(IDEMPOTENCY_KEY_MAX_LENGTH=8)
def test_an_over_long_key_raises_rather_than_truncating() -> None:
    """⚠️ Truncation would alias two distinct keys onto one record — the aliasing bug, politely.

    A Django `ValidationError`, so `urbenmend_exception_handler` renders the `400 VALIDATION_FAILED`
    §4.6 documents without this module importing DRF.
    """
    assert idempotency.normalize_key("12345678") == "12345678"

    with pytest.raises(ValidationError):
        idempotency.normalize_key("123456789")


def test_the_length_bound_comes_from_settings() -> None:
    """NFR-11 — the bound is deployment configuration, not a literal in the module.

    Read at call time rather than bound at import, so `override_settings` reaches it. The same trap
    T1.8 recorded for the throttle rates, where a class attribute made the 429 path untestable.
    """
    with override_settings(IDEMPOTENCY_KEY_MAX_LENGTH=4), pytest.raises(ValidationError):
        idempotency.normalize_key("12345")
    assert idempotency.normalize_key("12345") == "12345"


# ---------------------------------------------------------------------------------------
# fingerprint — §4.6's normalized comparison
# ---------------------------------------------------------------------------------------


def test_key_order_does_not_change_the_fingerprint() -> None:
    """§4.6 — normalized, not raw bytes. A reordering client is making the same submission."""
    assert idempotency.fingerprint({"a": 1, "b": 2}) == idempotency.fingerprint({"b": 2, "a": 1})


def test_different_content_changes_the_fingerprint() -> None:
    """The other half — otherwise reuse is undetectable and a second submission is silently lost."""
    assert idempotency.fingerprint({"a": 1}) != idempotency.fingerprint({"a": 2})


def test_the_fingerprint_does_not_contain_its_inputs() -> None:
    """⚠️ NFR-12 — a report's description is the citizen's own account of where they are.

    The digest is the one-way step that keeps it out of Redis. A "canonical JSON" scheme that stored
    the rendering itself would compare correctly and leak everything.
    """
    secret = "pothole outside 14 Green Road"
    digest = idempotency.fingerprint({"description": secret})

    assert secret not in digest
    assert len(digest) == 64


# ---------------------------------------------------------------------------------------
# reserve / complete / release — §4.6's three outcomes
# ---------------------------------------------------------------------------------------


def test_a_first_use_reserves_the_key() -> None:
    assert isinstance(_reserve(), idempotency.Reservation)


def test_a_completed_key_replays_its_stored_payload() -> None:
    reservation = _reserve()
    assert isinstance(reservation, idempotency.Reservation)
    idempotency.complete(reservation, payload={"report_id": "abc"})

    replay = _reserve()

    assert isinstance(replay, idempotency.Replay)
    assert replay.payload == {"report_id": "abc"}


def test_an_unfinished_original_is_in_progress() -> None:
    """The window between `reserve()` and `complete()` — where a double-tap actually lands."""
    _reserve()

    with pytest.raises(IdempotencyInProgress):
        _reserve()


def test_a_different_fingerprint_is_reuse_whatever_the_state() -> None:
    """⚠️ The fingerprint is checked before the state, and both states must agree.

    The client's remedy for reuse is a new key, and that is true whether the original finished or
    not — reporting an in-flight mismatch as `IN_PROGRESS` would tell them to retry a request that
    can never be honoured under that key.
    """
    reservation = _reserve()
    assert isinstance(reservation, idempotency.Reservation)

    with pytest.raises(IdempotencyKeyReused):
        _reserve(request_fingerprint=OTHER_PRINT)

    idempotency.complete(reservation, payload={"report_id": "abc"})

    with pytest.raises(IdempotencyKeyReused):
        _reserve(request_fingerprint=OTHER_PRINT)


def test_release_frees_the_key_for_a_corrected_retry() -> None:
    """§4.6 — a failed request does not consume its key.

    ⚠️ Asserted with a *different* fingerprint, which is the real client behaviour: they failed
    validation, so they are fixing the body. A same-fingerprint retry would pass even if `release()`
    only downgraded the record instead of deleting it.
    """
    reservation = _reserve()
    assert isinstance(reservation, idempotency.Reservation)
    idempotency.release(reservation)

    assert isinstance(_reserve(request_fingerprint=OTHER_PRINT), idempotency.Reservation)


def test_keys_are_scoped_per_user_and_per_operation() -> None:
    """§4.6 — "scoped **per user and per operation**".

    Per-user stops one citizen's key colliding with another's. Per-operation stops a client that
    mints one key per user action from having one endpoint's response replayed for a different one.
    """
    _reserve()

    assert isinstance(_reserve(user_id="another-user"), idempotency.Reservation)
    assert isinstance(_reserve(scope="tests.other"), idempotency.Reservation)


def test_a_vanished_record_reads_as_a_fresh_claim() -> None:
    """⚠️ A record that expires between `add()` and `get()` must not answer `409`.

    Nobody holds that key by then, so rejecting a legitimate submission is strictly worse than one
    duplicate in a race that is already outside the retention window. The window is microseconds
    wide, which is why it is driven with a stub rather than a sleep — an `IDEMPOTENCY_*_SECONDS=0`
    test would exercise django-redis's "delete instead of setting a non-positive TTL" branch and
    never reach this code at all.
    """
    with patch("urbenmend.api.idempotency.cache") as fake_cache:
        fake_cache.add.return_value = False
        fake_cache.get.return_value = None

        outcome = _reserve()

        assert isinstance(outcome, idempotency.Reservation)
        # ⚠️ Re-claimed, not merely allowed through: without the `set()` the caller would hold no
        # record, so a concurrent duplicate would also see an empty slot and also proceed.
        fake_cache.set.assert_called_once()


def test_an_unreadable_record_reads_as_a_fresh_claim() -> None:
    """The same posture for a record this module did not write, or wrote in an older shape.

    A deploy that changes the stored structure would otherwise turn every in-flight key into an
    `AttributeError` — a `500` on submission, for every retrying client, until the window drained.
    """
    reservation = _reserve()
    assert isinstance(reservation, idempotency.Reservation)
    cache.set(reservation.cache_key, "a value from some other shape", timeout=60)

    assert isinstance(_reserve(), idempotency.Reservation)


@override_settings(IDEMPOTENCY_IN_PROGRESS_SECONDS=7, IDEMPOTENCY_RETENTION_SECONDS=99)
def test_the_two_ttls_come_from_settings_and_differ() -> None:
    """NFR-11, and the reason there are two windows rather than one.

    ⚠️ **An unfinished request must hold its key for far less time than a finished one.** The
    in-progress TTL is the backstop for a process killed between `reserve()` and
    `complete()`/`release()`; at retention length, one crash would lock a citizen's key for a day.
    """
    with patch("urbenmend.api.idempotency.cache") as fake_cache:
        fake_cache.add.return_value = True

        reservation = _reserve()
        assert isinstance(reservation, idempotency.Reservation)
        assert fake_cache.add.call_args.kwargs["timeout"] == 7

        idempotency.complete(reservation, payload={"report_id": "abc"})
        assert fake_cache.set.call_args.kwargs["timeout"] == 99


# ---------------------------------------------------------------------------------------
# What reaches Redis (NFR-12)
# ---------------------------------------------------------------------------------------


def test_the_clients_key_never_appears_in_a_cache_key() -> None:
    """⚠️ Cache keys surface in `redis-cli KEYS`, slow logs and dumps.

    The reasoning `AuthIdentityRateThrottle` records for hashing an email applies to a
    client-supplied idempotency key: it is a bearer token for a stored response, and anyone who reads
    it out of a key listing can replay that response.
    """
    reservation = _reserve(key="an-obviously-recognisable-key")

    assert isinstance(reservation, idempotency.Reservation)
    assert "an-obviously-recognisable-key" not in reservation.cache_key
    assert USER not in reservation.cache_key


def test_the_cache_key_stays_scannable_by_operation() -> None:
    """The readable prefix is deliberate: operations must be able to scope a `SCAN` to one endpoint.

    Everything identifying is inside the digest; the namespace and scope stay in the clear.
    """
    reservation = _reserve()

    assert isinstance(reservation, idempotency.Reservation)
    assert reservation.cache_key.startswith(f"idempotency:{SCOPE}:")


def test_a_completed_record_stores_the_digest_not_the_request() -> None:
    """Only the fingerprint and the response payload are retained — never the submission itself."""
    reservation = _reserve()
    assert isinstance(reservation, idempotency.Reservation)
    idempotency.complete(reservation, payload={"report_id": "abc"})

    record = cache.get(reservation.cache_key)

    assert record == {"state": "completed", "fingerprint": PRINT, "payload": {"report_id": "abc"}}


# ---------------------------------------------------------------------------------------
# Concurrency — R-2's mandated mitigation
# ---------------------------------------------------------------------------------------


def test_only_one_of_two_simultaneous_callers_reserves_the_key() -> None:
    """⚠️ **R-2's mitigation, and the reason `cache.add()` is not a `get()`/`set()` pair.**

    `docs/08-coding-workflow.md`: "For concurrency-sensitive tasks (… T2.3 idempotency …): write a
    test that fires two concurrent requests and asserts no duplicate is created."

    A `get()`-then-`set()` implementation reads as equivalent and is not: both threads would find an
    empty slot, both would reserve, and both would go on to write a Report — precisely the double-tap
    BR-5 exists to stop. `cache.add()` is Redis `SETNX`, so exactly one wins.

    ⚠️ **The `Barrier` is what makes this a race rather than two sequential calls.** Without it the
    threads almost always serialize and the test passes against the broken implementation. It is
    also why this exercises the store directly and not the endpoint: two threads sharing a test
    database transaction would deadlock long before they reached the interesting line.
    """
    threads = 8
    barrier = threading.Barrier(threads)
    reserved: list[idempotency.Reservation] = []
    refused: list[Exception] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=10)
        try:
            outcome = _reserve()
        except (IdempotencyInProgress, IdempotencyKeyReused) as exc:
            with lock:
                refused.append(exc)
            return
        with lock:
            assert isinstance(outcome, idempotency.Reservation)
            reserved.append(outcome)

    workers = [threading.Thread(target=attempt) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert len(reserved) == 1, "cache.add() must be atomic — exactly one caller may proceed"
    assert len(refused) == threads - 1
    assert all(isinstance(exc, IdempotencyInProgress) for exc in refused)
