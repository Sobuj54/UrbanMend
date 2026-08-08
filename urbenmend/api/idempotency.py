"""
`Idempotency-Key` resolution (T2.3, BR-5, API §4.6).

BR-5: "duplicate submissions from one action (double-tap/retry) resolve to a **single** Report".
The failure this prevents is not cosmetic — a duplicate Report clusters into the same Issue and
inflates the corroboration count FR-16 reads as "how many people are affected", so one citizen's
dropped connection reads as two complaints.

**Why this lives in `api/` rather than in `reporting/`.** §4.6 says `POST /reports` "and other
duplicate-sensitive creates"; the mechanism is per-user, per-operation and payload-aware, and none
of that is reporting-specific. `reporting/services.py` supplies the scope, the user and the
fingerprint inputs — this module owns the protocol.

**The protocol is `reserve` → `complete` / `release`,** and it is three calls rather than one
because the interesting window is *while the original request is still running*:

    reservation = reserve(...)      # atomic; loses to a concurrent caller rather than racing it
    ...do the work...
    complete(reservation, payload=...)   # on success — from `transaction.on_commit`
    release(reservation)                 # on failure — the key is not consumed (§4.6)

⚠️ **`cache.add()` is the whole concurrency story.** It is Redis `SETNX`: exactly one of two
simultaneous callers gets `True`. A `get()`-then-`set()` pair reads as equivalent and is not —
both callers would see an empty slot and both would write a Report, which is precisely the
double-tap BR-5 exists to stop, and it only fails under the concurrency that never happens locally.

⚠️ **Neither the key nor the request content is stored in plaintext.** Cache keys surface in
`redis-cli KEYS`, slow logs and dumps (NFR-12, the reasoning `AuthIdentityRateThrottle` records).
The client's key is hashed into the cache key, and the request is reduced to a SHA-256 fingerprint
— a report's description and coordinates are the citizen's own account of where they are.

[doc: API §4.6, §4.2, §6.3; BR-5; plan T2.3 "Redis-backed, resolved in the service before the write"]
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError

from urbenmend.api.exceptions import IdempotencyInProgress, IdempotencyKeyReused

# Namespace for every record this module writes, so an operator can scope a `KEYS`/`SCAN` to it
# without matching session or throttle entries.
_CACHE_PREFIX = "idempotency"

_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"

# ⚠️ **Policy, not spec — `api-conventions.md` lists "idempotency-key retention window" under
# "Not specified — do not invent".** These are the module's fallbacks; `settings/base.py` sets the
# real values from the environment (NFR-11), and the reasoning for each number lives there. Same
# posture as T1.2's code TTL and T1.8's throttle rates: choose defensible values, keep them in
# config, and label them so nobody later mistakes them for a contract.
DEFAULT_RETENTION_SECONDS = 86_400
DEFAULT_IN_PROGRESS_SECONDS = 60
DEFAULT_KEY_MAX_LENGTH = 255


@dataclass(frozen=True)
class Reservation:
    """A held key. Carries its own fingerprint so `complete()` needs no second cache read."""

    cache_key: str
    fingerprint: str


@dataclass(frozen=True)
class Replay:
    """A completed original, found again. `payload` is what the first request answered."""

    payload: dict[str, Any]


def _retention_seconds() -> int:
    return int(getattr(settings, "IDEMPOTENCY_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS))


def _in_progress_seconds() -> int:
    return int(getattr(settings, "IDEMPOTENCY_IN_PROGRESS_SECONDS", DEFAULT_IN_PROGRESS_SECONDS))


def _key_max_length() -> int:
    return int(getattr(settings, "IDEMPOTENCY_KEY_MAX_LENGTH", DEFAULT_KEY_MAX_LENGTH))


def normalize_key(raw: str | None) -> str | None:
    """Header value → the key to use, or `None` for "no idempotency requested" (§4.6).

    ⚠️ **A blank or whitespace-only header is `None`, not a key.** Some HTTP clients send an empty
    header for an unset value; treating `""` as a real key would make every such client share one
    bucket, so the second citizen to submit would get the first one's report back. Absence must
    mean absence.

    ⚠️ **Over-long keys are rejected rather than truncated.** Truncating silently collapses two
    distinct keys into one — the same aliasing bug, arrived at politely. §4.6 documents the `400`.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > (limit := _key_max_length()):
        raise ValidationError(
            {"Idempotency-Key": f"Must be at most {limit} characters."},
            code="INVALID",
        )
    return key


def fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical rendering of the *normalized* request (§4.6).

    ⚠️ **Normalized, not raw bytes.** §4.6 fixes this: a client that reorders its JSON keys, adds
    whitespace, or writes `90.3990` instead of `90.399` is making the same submission, and a
    byte-level comparison would answer `409 IDEMPOTENCY_KEY_REUSED` to a perfectly ordinary retry —
    turning the safety net into a fault. Callers pass the post-validation values they are about to
    write, so the fingerprint covers exactly what determines the resulting row.

    ⚠️ **The digest, never the inputs, is what gets stored.** A report's description and coordinates
    are PII (NFR-12); this is the one-way step that keeps them out of Redis.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cache_key(*, scope: str, user_id: Any, key: str) -> str:
    """`idempotency:<scope>:<digest>` — scoped per user **and** per operation (§4.6).

    ⚠️ **Both scope and user are inside the digest, not just beside it.** Per-user is what stops
    one citizen's key colliding with another's; per-operation is what stops the same key, sent to
    two endpoints by a client that generates one per user action, replaying an unrelated response.
    `scope` also stays in the readable prefix so operations can scan one endpoint's records.
    """
    material = f"{scope}\x1f{user_id}\x1f{key}".encode()
    return f"{_CACHE_PREFIX}:{scope}:{hashlib.sha256(material).hexdigest()[:40]}"


def reserve(
    *,
    scope: str,
    user_id: Any,
    key: str,
    request_fingerprint: str,
) -> Reservation | Replay:
    """Claim the key, or resolve what the last holder did with it (§4.6's three outcomes).

    Returns a `Reservation` when this caller may proceed, or a `Replay` carrying the original
    response payload. Raises `IdempotencyKeyReused` (`409`) when the key is bound to a different
    request, and `IdempotencyInProgress` (`409`) when the original has not finished.

    ⚠️ **The fingerprint is checked before the state.** A key reused for *different* content is a
    `409` whether the original finished or not — the client's remedy (use a new key) is the same
    either way, and reporting it as merely "in progress" would tell them to retry a request that
    can never be honoured under that key.

    ⚠️ **A vanished record is treated as a fresh claim, not an error.** `add()` can fail on a
    record whose TTL expires before the read a microsecond later. Answering `409` for a key nobody
    holds would reject a legitimate submission; re-claiming it costs at worst one duplicate in a
    race that is already outside the retention window.
    """
    cache_key = _cache_key(scope=scope, user_id=user_id, key=key)
    reservation = Reservation(cache_key=cache_key, fingerprint=request_fingerprint)
    pending = {"state": _IN_PROGRESS, "fingerprint": request_fingerprint}

    if cache.add(cache_key, pending, timeout=_in_progress_seconds()):
        return reservation

    existing = cache.get(cache_key)
    if not isinstance(existing, dict):
        cache.set(cache_key, pending, timeout=_in_progress_seconds())
        return reservation

    if existing.get("fingerprint") != request_fingerprint:
        raise IdempotencyKeyReused
    if existing.get("state") != _COMPLETED:
        raise IdempotencyInProgress
    return Replay(payload=dict(existing.get("payload") or {}))


def complete(reservation: Reservation, *, payload: dict[str, Any]) -> None:
    """Record the result a replay will be answered with, for the retention window.

    ⚠️ **Call this from `transaction.on_commit`, never inline.** The record is a promise that a row
    exists; writing it before the transaction commits means a replay can hand a client the id of a
    row that was rolled away — user-visible, and for up to the whole retention window. Same rule,
    and the same reason, as the classification enqueue (Arch §4.1, async-worker.md).
    """
    cache.set(
        reservation.cache_key,
        {
            "state": _COMPLETED,
            "fingerprint": reservation.fingerprint,
            "payload": payload,
        },
        timeout=_retention_seconds(),
    )


def release(reservation: Reservation) -> None:
    """Give the key back after a failed attempt — §4.6: a failure does not consume the key.

    ⚠️ **Without this a validation error would lock the key for the whole retention window**, and
    the client's natural next move — fix the body, retry with the same key — would come back as
    `409 IDEMPOTENCY_KEY_REUSED`. The short in-progress TTL is only the backstop for a process that
    dies before reaching either branch.
    """
    cache.delete(reservation.cache_key)
