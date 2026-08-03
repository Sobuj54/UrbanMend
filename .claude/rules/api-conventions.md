---
description: HTTP contract rules — envelopes, status codes, pagination, filtering
paths:
  - "**/serializers.py"
  - "**/views.py"
  - "**/viewsets.py"
  - "**/urls.py"
  - "**/pagination.py"
  - "**/exceptions.py"
---

# API conventions

Source: `docs/04-api-specification.md`. **That spec is authoritative over the implementation** — if
code must differ, the spec is amended first.

## Two known DRF divergences (customise, don't accept defaults — plan T0.6)

1. **DRF serializers emit `snake_case`; the contract is `camelCase`.** An explicit renaming layer is
   required. The docs call this "the single easiest way for the implementation to silently drift".
2. **DRF's built-in cursor pagination emits a different shape** than the `data`/`page`/`meta`
   envelope. A custom pagination class is required.

## Shape

Base URL `https://{host}/api/v1`. URI versioning. Plural lowercase resource nouns. JSON only
(`application/json`, `charset=utf-8`). Timestamps ISO-8601 UTC (`2026-07-22T10:15:30Z`).
Coordinates `{ "lng": ..., "lat": ... }`; map payloads are GeoJSON `FeatureCollection`s via
`GeoFeatureModelSerializer`. Identifiers are opaque server-generated strings — never sequential or
guessable in URLs (no IDOR). `PATCH` for partial mutation; `PUT` reserved for full replacement.

Collections (all list endpoints):

```json
{
  "data": [ /* resource objects */ ],
  "page": { "nextCursor": "opaque-or-null", "prevCursor": "opaque-or-null", "limit": 20 },
  "meta": { "count": 20 }
}
```

Single resources return the bare object, no envelope.

Errors (uniform for every error):

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Human-readable summary (localized where applicable).",
    "details": [ { "field": "location", "issue": "REQUIRED", "message": "A location is required." } ],
    "traceId": "correlation-id-for-support"
  }
}
```

## Status codes

| Code | Use |
|---|---|
| 200 / 201 / 202 / 204 | read-update / created (+`Location`) / accepted for async / no body |
| 400 | malformed body, unknown query param |
| 401 / 403 | unauthenticated / authenticated but not permitted |
| 404 | absent **or hidden from this caller** (avoid existence leaks) |
| 409 | state conflict — `NOT_EDITABLE`, `INVALID_TRANSITION`, `ALREADY_CONFIRMED`, idempotency replay |
| 410 | removed by moderation (FR-31) |
| 413 / 415 | image too large / disallowed file type (FR-7) |
| 422 | business-rule violation — e.g. `OUT_OF_CITY` |
| 429 | rate limited (`Retry-After`) |
| 500 / 503 | server error / dependency unavailable |

Base codes: `UNAUTHENTICATED` `FORBIDDEN` `NOT_FOUND` `VALIDATION_FAILED` `RATE_LIMITED`
`CONFLICT` `INTERNAL`. All protected endpoints implicitly return 401 and 429.

## Query params

- **Pagination** — cursor-based, `?limit=` (default 20, max 100) + `?cursor=`. **Mandatory on all
  collections** (NFR-2). Offset pagination is wrong here: the authority queue is sorted and mutated
  concurrently, so offsets skip/repeat rows.
- **Filtering** — explicit params per resource; multiple values comma-separated
  (`?severity=high,medium`). Unknown params → `400`.
- **Sorting** — `?sort=` against a documented allowlist per resource; leading `-` = descending.
  Issues default to **severity DESC, then age**; reports `-createdAt`; comments `createdAt`;
  notifications `-createdAt`.
- **Search** — `?q=` free-text, bilingual. Spatial search uses `?nearLng=&nearLat=&radiusM=` or
  `?bbox=`, never `q`.
- **Field selection** — `?fields=` allowlist; additive, never required.

## Headers

`Idempotency-Key` on duplicate-sensitive creates (`POST /reports`) — a replay returns the original
result rather than creating a duplicate (BR-5). `Accept-Language: bn|en` for localized strings.
`RateLimit-Limit`/`-Remaining`/`-Reset` on limited endpoints. `Deprecation`/`Sunset` when retiring
an endpoint.

## Hard rules

- No `POST /issues` — Issues form via async clustering only.
- Status-events and audit-events are read-only: no POST/PATCH/DELETE (C-9, BR-31).
- No hard-delete endpoints; no `DELETE /users/{id}` (deletion anonymizes, C-14).
- No outbound webhooks to external/government systems (PRD §2.2 non-goal).
- Derived data (corroboration count, proximity, classification, rationale) is read-only to all
  clients. Severity is settable only by an Authority override, which **never overwrites** the
  computed value — both are retained and shown.
- Login, registration, and password-reset responses are generic — no user enumeration.
- Reject unknown fields where strictness matters (config endpoints).
- Additive changes ship within `v1`; clients must ignore unknown fields and enum values.

**Not specified — do not invent:** numeric rate limits and windows, the CSRF header name,
idempotency-key retention window, session TTL, validation library.
