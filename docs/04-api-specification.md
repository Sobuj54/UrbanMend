# UrbanMend — Backend REST API Specification

> The production-ready HTTP contract for the UrbanMend backend, derived entirely from the approved PRD, Architecture, and Domain Model.

| | |
|---|---|
| **Document** | `docs/04-api-specification.md` |
| **Version** | 1.1 (Q2/Q4/Q7/Q8/Q9/DM-Q5/Q7/Q8 resolved) |
| **Status** | Planning phase — pending stakeholder sign-off |
| **Author role** | Principal Backend Architect |
| **Date** | 2026-07-22 |
| **Source of truth** | `01-prd.md` (v1.1) · `02-architecture.md` (v1.0) · `03-data-model.md` (v1.0) — all approved |
| **Scope** | Backend REST API only. Client/UI is a consumer. |

### Ground rules
- The three approved docs are the **single source of truth.** This spec introduces **no new business rules or features.** Every endpoint traces to a `FR-x`/`NFR-x` or a Domain rule (`BR-x`).
- Where behavior depends on an **unresolved open question**, the endpoint **references it** (PRD `❓Qx`, Domain `DM-Qx`, `ASSUMP-x`) and does **not** invent an answer.
- **No implementation code.** Schemas are described as JSON contracts, not database or language artifacts.

---

## 1. API Design Principles & Conventions

### 1.1 Principles
- **Resource-oriented REST over HTTPS.** Nouns as resources, HTTP verbs as actions, appropriate status codes.
- **Issue-centric, not Report-centric.** Triage, severity, status, assignment, comments, and confirmations hang off `/issues`; `/reports` covers intake and a citizen tracking their own submission (Domain: Report vs Issue separation).
- **Server is authoritative.** Derived data (corroboration count, proximity context, classification result, severity rationale) is **read-only** to all clients (C-10, BR-22/24). No client can set severity except an Authority override (FR-20).
- **Predictable & consistent.** One error envelope, one pagination style, one date format, uniform naming.
- **Secure by default.** Authorization enforced server-side on every mutating and sensitive-read action (FR-3, BR-27); least privilege per the §4.2 role matrix.
- **Extensible.** Additive evolution (new fields, new filters) never breaks clients; versioning guards breaking changes.

### 1.2 Conventions
| Aspect | Convention |
|--------|-----------|
| Base URL | `https://{host}/api/v1` |
| Format | JSON only (`Content-Type: application/json`); `charset=utf-8` (Bangla/English, NFR-8) |
| Resource naming | Plural, kebab-free lowercase nouns: `/reports`, `/issues`, `/notification-preferences` |
| Identifiers | Opaque server-generated string IDs (UUID-style); never sequential/guessable in URLs |
| Timestamps | ISO-8601 UTC (`2026-07-22T10:15:30Z`); clients localize (NFR-8) |
| Coordinates | GeoJSON conventions: `{ "lng": ..., "lat": ... }`; map payloads as GeoJSON `FeatureCollection` |
| Field casing | `camelCase` in JSON bodies |
| Nulls | Omit unknown/derived-not-yet-ready fields or return explicit `null`; documented per field |
| Idempotency | Unsafe creates that must not duplicate accept an `Idempotency-Key` header (BR-5) |
| Partial update | `PATCH` for partial mutations; `PUT` reserved for full replacement (rarely used here) |
| Localization | `Accept-Language: bn` \| `en` influences human-readable strings where the server localizes |

### 1.3 Standard collection envelope
All list endpoints return:
```json
{
  "data": [ /* resource objects */ ],
  "page": { "nextCursor": "opaque-or-null", "prevCursor": "opaque-or-null", "limit": 20 },
  "meta": { "count": 20 }
}
```
Single-resource endpoints return the bare resource object (no envelope).

---

## 2. Authentication Strategy

**Approved mechanism (Architecture §8): server-validated sessions**, chosen over stateless JWT for reliable **immediate revocation** (moderation, deprovisioning). No cross-service token sharing is needed at this scale.

- On successful login the server issues an **opaque session token** in a **`Secure`, `HttpOnly`, `SameSite` cookie**. The token is not readable by JavaScript.
- **CSRF protection:** because auth rides in a cookie, state-changing requests require a CSRF token (double-submit cookie or header token). Safe methods (`GET`/`HEAD`) are exempt.
- **Session revocation** is immediate server-side (logout, suspension, deprovisioning) — supports BR-25/BR-33 and moderation.
- **Verification** (FR-1): phone via OTP, email via link/code. Unverified accounts cannot receive notifications on an unverified channel (BR-30) and have limited capability.
- **Authority/Admin** may be required to use **2FA** (FR-4, optional per policy).
- **Anonymous access** to write endpoints: **Q4 RESOLVED — login required.** All submissions require a Citizen session. Anonymous write access is not supported.
- **Public reads**: **Q7 RESOLVED — map and issue list are publicly visible** to unauthenticated users.

> *Note:* mechanics of OTP/token storage are implementation (Architecture §8) and out of scope here; this spec defines only the request/response contract.

### 2.1 Authorization model
Every protected endpoint states required **role** (Citizen / Authority / Admin) and any **conditions** (own-resource, authority category-scope BR-26, pre-triage editability, mandatory reason). Authorization is enforced in the service layer (FR-3).

---

## 3. Resource Structure

| Resource | Base path | Backing entity (Domain) | Primary FRs |
|----------|-----------|--------------------------|-------------|
| Auth/session | `/auth` | User (session) | FR-1, FR-4 |
| Users | `/users` | User | FR-1, FR-2, FR-3, P6 |
| Reports | `/reports` | Report | FR-5, FR-6, FR-8, FR-11 |
| Media | `/reports/{id}/media`, `/media` | Media | FR-7, P3 |
| Issues | `/issues` | Issue | FR-14–20, FR-22, FR-24, FR-25 |
| Confirmations | `/issues/{id}/confirmations` | Confirmation | FR-16, S3 |
| Comments | `/issues/{id}/comments` | Comment | FR-24 |
| Status history | `/issues/{id}/status-events` | Status Event | §6.3, FR-32 |
| Map | `/map/issues` | Issue (projection) | FR-23 |
| Analytics | `/analytics/*` | derived | FR-26 |
| Categories | `/categories` | Category | FR-30, §6.2 |
| POIs | `/pois` | Point of Interest | FR-17, FR-30 |
| Severity keywords | `/severity-keywords` | Severity Keyword | FR-13a, FR-30 |
| Clustering rules | `/clustering-rules` | Clustering Rule | FR-18, FR-30 |
| Notifications | `/notifications`, `/notifications/stream` | Notification | FR-27, FR-29, ASSUMP-3 |
| Notification prefs | `/notification-preferences` | Notification Preference | FR-28 |
| Audit | `/audit-events` | Audit Event | FR-32 |
| Exports | `/exports` | derived | NFR-12 |
| Reference/meta | `/meta/*`, `/health` | config/system | NFR-9/11 |

---

## 4. Cross-Cutting Contracts

### 4.1 Error response model
Uniform envelope for all errors:
```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Human-readable summary (localized where applicable).",
    "details": [
      { "field": "location", "issue": "REQUIRED", "message": "A location is required." }
    ],
    "traceId": "correlation-id-for-support"
  }
}
```

### 4.2 Standard status codes
| Code | Meaning / usage |
|------|-----------------|
| 200 OK | Successful read/update |
| 201 Created | Resource created (returns resource + `Location` header) |
| 202 Accepted | Accepted for **async** processing (e.g. report submitted, triage pending — NFR-3) |
| 204 No Content | Successful with no body (e.g. delete, mark-read) |
| 400 Bad Request | Malformed syntax / unparseable body |
| 401 Unauthorized | Missing/invalid session |
| 403 Forbidden | Authenticated but not permitted (role/scope) |
| 404 Not Found | Resource absent, or hidden from this caller |
| 409 Conflict | State conflict (invalid status transition BR-16, duplicate confirmation BR-23, idempotency replay) |
| 410 Gone | Resource removed by moderation (FR-31) |
| 413 Payload Too Large | Image exceeds limit (FR-7) |
| 415 Unsupported Media Type | Disallowed file type (FR-7) |
| 422 Unprocessable Entity | Semantically invalid (business-rule violation) |
| 429 Too Many Requests | Rate limit exceeded (NFR-13, FR-33) |
| 500 / 503 | Server error / dependency unavailable (LLM/geocoder degraded — degrades, not blocks, per NFR-4) |

### 4.3 Common error codes (referenced per endpoint as "standard errors")
`UNAUTHENTICATED (401)` · `FORBIDDEN (403)` · `NOT_FOUND (404)` · `VALIDATION_FAILED (400/422)` · `RATE_LIMITED (429)` · `CONFLICT (409)` · `INTERNAL (500)`.

### 4.4 Pagination, filtering, sorting, search
- **Pagination — cursor-based (default).** `?limit=` (default 20, max 100) + `?cursor=`. Cursor pagination is chosen because the authority queue is sorted and mutated concurrently; offset pagination would skip/repeat rows. Response carries `page.nextCursor`/`prevCursor`. Pagination is **mandatory** on all collections (NFR-2).
- **Filtering** — explicit query params per resource (e.g. `?category=roads&severity=high&status=in_progress`). Multiple values comma-separated (`?severity=high,medium`). Unknown params → `400`.
- **Sorting** — `?sort=` with a documented allowlist per resource (e.g. issues: `severity`, `age`, `-createdAt`). Default per resource stated below. Leading `-` = descending.
- **Search** — `?q=` free-text over documented fields (e.g. report description); bilingual (NFR-8). Spatial search uses explicit geo params (`?nearLng=&nearLat=&radiusM=` or `?bbox=`), not `q`.
- **Field selection (optional, extensibility)** — `?fields=` allowlist to trim payloads; additive, never required.

### 4.5 Rate limiting (NFR-13, FR-33, FR-4)
- Applied per-identity (session) and per-IP for anonymous/auth endpoints.
- Tighter buckets on: login/OTP (brute-force, FR-4), report submission (spam, FR-33), and anything that can trigger LLM calls (cost, NFR-13/RISK-3).
- Response headers on every limited endpoint: `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`; `429` includes `Retry-After`.
- Exceeding LLM cost/rate caps does **not** fail submission — triage degrades to the keyword fallback (FR-13a); the API still returns `202`.

### 4.6 Idempotency
`POST /reports` (and other duplicate-sensitive creates) accept `Idempotency-Key`. A replay with the same key returns the original result (`200`/`201`) rather than creating a duplicate (BR-5). Keys are scoped per user and retained for a bounded window.

---

## 5. API Versioning Strategy
- **URI versioning: `/api/v1`.** Chosen for explicitness, cache-friendliness, and ease of routing.
- **Additive changes are non-breaking** and ship within `v1` (new optional fields, new endpoints, new enum values behind capability flags). Clients MUST ignore unknown fields.
- **Breaking changes** (removing/renaming fields, changing semantics, e.g. adding **Critical** severity — Q2 RESOLVED: Critical is now part of the enum) are handled within `v1` when additive (new enum value clients must ignore-unknown); a `/api/v2` is triggered only if existing clients must change behavior to handle it. Clients MUST ignore unknown enum values to stay forward-compatible.
- **Deprecation signaling:** `Deprecation` and `Sunset` response headers on affected endpoints.

---

## 6. Endpoint Specifications

> Format per endpoint: Method · URL · Purpose · Auth · Authorization · Request (params/body) · Response · Errors. "Standard errors" = §4.3. All protected endpoints implicitly return `401` if unauthenticated and `429` if rate-limited.

---

### 6.1 Authentication & Session — `/auth`

#### `POST /auth/register`
- **Purpose:** Register a citizen account (FR-1).
- **Auth:** None.
- **Authorization:** Public.
- **Body:**
```json
{ "email": "user@example.com", "phone": "+8801XXXXXXXXX", "password": "…", "preferredLanguage": "bn" }
```
(At least one of `email`/`phone` required.)
- **Response `201`:** `{ "userId": "...", "verificationRequired": true, "channels": ["email"] }`
- **Errors:** `VALIDATION_FAILED`, `409 CONFLICT` (identity already registered), standard.

#### `POST /auth/verify`
- **Purpose:** Confirm email/phone via OTP or link code (FR-1).
- **Auth:** None (pre-session) or session.
- **Body:** `{ "channel": "phone", "code": "123456" }`
- **Response `200`:** `{ "verified": true }`
- **Errors:** `422` (code invalid/expired), `429` (too many attempts), standard.

#### `POST /auth/login`
- **Purpose:** Start a session (FR-1).
- **Auth:** None.
- **Body:** `{ "identifier": "email-or-phone", "password": "…" }`
- **Response `200`:** sets session cookie; body `{ "user": { "id", "role", "preferredLanguage" }, "requires2fa": false }`
- **Errors:** `401` (bad credentials — generic message, no user enumeration), `423`-equivalent surfaced as `403 ACCOUNT_LOCKED` (FR-4), `429`, standard.

#### `POST /auth/2fa/verify`
- **Purpose:** Complete 2FA for authority/admin (FR-4).
- **Auth:** Partial session (post-password).
- **Body:** `{ "code": "…" }`
- **Response `200`:** full session established.
- **Errors:** `422`, `429`, standard.

#### `POST /auth/logout`
- **Purpose:** Revoke current session (Architecture §8).
- **Auth:** Session. **Authorization:** Self.
- **Response `204`.**

#### `POST /auth/password/forgot` · `POST /auth/password/reset`
- **Purpose:** Initiate and complete password reset (FR-1).
- **Auth:** None.
- **Bodies:** `{ "identifier": "…" }` → then `{ "resetToken": "…", "newPassword": "…" }`
- **Responses:** `202` (always generic, no enumeration) / `200`.
- **Errors:** `422` (token invalid/expired), `429`, standard.

---

### 6.2 Users — `/users`

#### `GET /users/me`
- **Purpose:** Current user's profile, role, scope, verification state.
- **Auth:** Session. **Authorization:** Self.
- **Response `200`:**
```json
{ "id":"...", "role":"authority", "email":"...", "phone":"...",
  "verified": { "email": true, "phone": false },
  "categoryScope": ["roads","water_drainage"], "preferredLanguage":"en", "status":"active" }
```
- **Errors:** standard.

#### `PATCH /users/me`
- **Purpose:** Update own profile (contact, language). Contact changes re-trigger verification.
- **Auth:** Session. **Authorization:** Self.
- **Body:** `{ "phone": "…", "preferredLanguage": "bn" }`
- **Response `200`:** updated profile.
- **Errors:** `409` (identity in use), `VALIDATION_FAILED`, standard.

#### `DELETE /users/me`
- **Purpose:** Request account deletion; **PII anonymized**, public Issue records retained (P6, BR-33, C-14).
- **Auth:** Session. **Authorization:** Self.
- **Response `202`** (anonymization may be async).
- **Errors:** standard.

#### `GET /users` *(admin)*
- **Purpose:** List/search accounts for administration.
- **Auth:** Session. **Authorization:** Admin.
- **Params:** `?role=&status=&q=` + pagination.
- **Response `200`:** collection of user summaries.
- **Errors:** `FORBIDDEN`, standard.

#### `POST /users/authorities` *(admin)*
- **Purpose:** **Provision an Authority account** and set category scope (FR-2, BR-25). Grant is audited (FR-32).
- **Auth:** Session. **Authorization:** Admin.
- **Body:** `{ "email":"...", "categoryScope":["roads"], "requireTwoFactor": true }`
- **Response `201`:** authority user summary.
- **Errors:** `409`, `VALIDATION_FAILED`, `FORBIDDEN`, standard.

#### `PATCH /users/{id}` *(admin)*
- **Purpose:** Change role/scope/status (suspend, deprovision) — each change audited (FR-32).
- **Auth:** Session. **Authorization:** Admin.
- **Body (examples):** `{ "status":"suspended" }` | `{ "categoryScope":["roads","electrical"] }`
- **Response `200`.**
- **Errors:** `422`, `FORBIDDEN`, standard.

> No `DELETE /users/{id}` hard-delete: deletion is anonymization (C-14). Admin deprovisions via `PATCH … {status:"deprovisioned"}`.

---

### 6.3 Reports — `/reports`

#### `POST /reports`
- **Purpose:** Submit a report; persists immediately, triage runs async (FR-5, NFR-3, Architecture §4).
- **Auth:** Session required (Q4 RESOLVED: login required for all submissions).
- **Authorization:** Citizen.
- **Headers:** `Idempotency-Key` (BR-5).
- **Body:**
```json
{
  "description": "Large pothole near the hospital gate",
  "location": { "lng": 90.399, "lat": 23.777 },
  "category": "roads",          // optional; AI suggests (FR-10). Client-supplied is a hint only.
  "mediaIds": ["media_abc"],    // pre-uploaded via §6.4, optional
  "language": "bn"
}
```
- **Validation:** location required (BR-2); at least one of {media, adequate description} (BR-3); location must be inside city boundary (BR-35, `422 OUT_OF_CITY`); category (if given) must be in taxonomy (C-2).
- **Response `202`:**
```json
{ "reportId":"rep_123", "status":"processing", "issueId": null,
  "classification": { "state":"pending" } }
```
`issueId` and `classification` populate after async triage (poll via `GET`, or receive a notification).
- **Errors:** `VALIDATION_FAILED`, `422 OUT_OF_CITY`, `413`/`415` (if inline media), `409` (idempotency replay returns original), standard.

#### `GET /reports/{id}`
- **Purpose:** Retrieve a report incl. classification result and its Issue link once triaged.
- **Auth:** Session or public (Q7 RESOLVED: public map and list visible to unauthenticated users).
- **Authorization:** Author (own), Authority (in scope), Admin, or public.
- **Response `200`:**
```json
{ "id":"rep_123", "authorId":"usr_x", "description":"…",
  "location": { "lng":90.399, "lat":23.777, "address":"…" },
  "media":[{ "id":"media_abc","thumbnailUrl":"…","state":"ready" }],
  "classification": { "category":"roads","severitySignal":"high","confidence":0.92,"source":"llm" },
  "issueId":"iss_456", "status":"triaged", "createdAt":"…" }
```
- **Errors:** `404`, `410` (moderated), `FORBIDDEN`, standard.

#### `GET /reports`
- **Purpose:** List **own** reports for tracking (FR-1 transparency); Authority/Admin variants via scope.
- **Auth:** Session.
- **Authorization:** Citizen → own only; Authority → in-scope; Admin → all.
- **Params:** `?status=&category=&q=&nearLng=&nearLat=&radiusM=` + pagination; `sort` default `-createdAt`.
- **Response `200`:** collection.
- **Errors:** standard.

#### `PATCH /reports/{id}`
- **Purpose:** Limited edits **before triage** (description/category correction), and citizen category override at submission (FR-11).
- **Auth:** Session. **Authorization:** Author, **only while pre-triage** (BR: report edit constrained); Authority/Admin may re-categorize (FR-11).
- **Body:** `{ "description":"…", "category":"water_drainage" }`
- **Response `200`.**
- **Errors:** `409 NOT_EDITABLE` (already triaged), `422`, `FORBIDDEN`, standard.

> Reports are not deleted by citizens; removal is a moderation action on the Issue/Report by Admin (FR-31) — see §6.13.

---

### 6.4 Media — `/reports/{id}/media`, `/media`

#### `POST /media`
- **Purpose:** Upload a photo; returns a media handle to attach to a report (FR-7). Server compresses, thumbnails, **strips EXIF/GPS by default** (P3) asynchronously.
- **Auth:** Session required (Q4 RESOLVED: login required).
- **Authorization:** Citizen.
- **Request:** `multipart/form-data` with `file`. Enforce size/type limits (FR-7).
- **Response `202`:**
```json
{ "id":"media_abc", "state":"processing", "thumbnailUrl": null }
```
- **Errors:** `413 PAYLOAD_TOO_LARGE`, `415 UNSUPPORTED_MEDIA_TYPE`, `422` (corrupt image), standard.

> *Alternative (ASSUMP, Architecture §10):* presigned direct-to-storage upload — `POST /media/upload-url` → client PUTs to storage → `POST /media/{id}/complete`. Offered as an extensibility path; not required at prototype scale.

#### `GET /media/{id}`
- **Purpose:** Fetch media metadata / processed URLs.
- **Auth/Authorization:** As owning report's visibility (Q7 RESOLVED: public).
- **Response `200`:** `{ "id","state":"ready","url":"…","thumbnailUrl":"…" }`
- **Errors:** `404`, `410` (moderated), standard.

#### `DELETE /media/{id}`
- **Purpose:** Remove a photo from a **pre-triage** report (author) or by moderation (admin).
- **Auth:** Session. **Authorization:** Author (pre-triage) or Admin (moderation, FR-31).
- **Response `204`.**
- **Errors:** `409 NOT_EDITABLE`, `FORBIDDEN`, standard.

---

### 6.5 Issues — `/issues` (the triage core)

#### `GET /issues`
- **Purpose:** The **authority work queue** — list Issues, severity-ranked (FR-22).
- **Auth:** Session or public (Q7 RESOLVED: public).
- **Authorization:** Authority → **within category scope** (BR-26); Admin → all; Citizen/public.
- **Params:**
  - Filters: `?category=&severity=high,medium&status=in_progress&assignedTo=me&bbox=minLng,minLat,maxLng,maxLat&nearLng=&nearLat=&radiusM=&openedAfter=&q=`
  - Sorting: `?sort=` allowlist `severity` (default, DESC), `age`, `-createdAt`, `corroborationCount`. Default: **severity DESC, then age** (FR-19, §5.1).
  - Pagination: cursor.
- **Response `200`:**
```json
{
  "data": [{
    "id":"iss_456", "primaryCategory":"roads",
    "severity": { "current":"high", "computed":"high", "overridden": null, "rationale":"phrases: 'danger','hospital'" },
    "status":"triaged", "assignedTo": null,
    "corroborationCount": 6,                      // derived, read-only (FR-16, C-10)
    "proximity": [ { "poiType":"hospital","name":"Dhaka Medical","distanceM":120 } ], // display-only (FR-17)
    "representativeLocation": { "lng":90.399,"lat":23.777 },
    "reportCount": 6, "openedAt":"…", "ageSeconds": 51600
  }],
  "page": { "nextCursor":"…","limit":20 }, "meta": { "count":20 }
}
```
- **Errors:** `FORBIDDEN` (out of scope), `VALIDATION_FAILED` (bad filter), standard.

#### `GET /issues/{id}`
- **Purpose:** Full issue detail incl. member reports, history pointers, context.
- **Auth:** Session or public (Q7 RESOLVED: public). **Authorization:** Authority (scope), Admin, Citizen/public.
- **Response `200`:** issue object (as above) plus `memberReports` (paged link), `severity` block, `duplicateOf` (nullable), public comments.
- **Errors:** `404`, `410`, `FORBIDDEN`, standard.

#### `GET /issues/{id}/reports`
- **Purpose:** Paginated member Reports of the issue (mass-event issues can be large — Architecture §14).
- **Auth/Authorization:** As issue visibility + scope.
- **Params:** pagination.
- **Response `200`:** collection of report summaries.

#### `PATCH /issues/{id}/status`
- **Purpose:** Advance/change Issue workflow status (FR-24, §6.3). Emits Status Event (BR-28) + Notification (BR-29).
- **Auth:** Session. **Authorization:** Authority (scope) / Admin only; **no one may advance past `Triaged` without this role** (BR-15).
- **Body:** `{ "toStatus":"in_progress", "reason":"crew dispatched", "publicNote": "Work has started." }`
  - `reason` required for `rejected`/`duplicate`/`insufficient_info`/`reopen` (BR-19).
  - For `duplicate`: `{ "toStatus":"duplicate", "duplicateOfIssueId":"iss_789", "reason":"…" }` (BR-17).
- **Validation:** transition must be legal per state machine (BR-16/C-7) else `409 INVALID_TRANSITION`.
- **Response `200`:** updated issue.
- **Errors:** `409 INVALID_TRANSITION`, `422` (missing required reason), `FORBIDDEN`, standard.
- **Q8 RESOLVED — Resolved definition:** Authority self-attestation. The transition to `resolved` requires only an authority action; no citizen confirmation is needed. **DM-Q8 RESOLVED — Reopen semantics:** Reopening creates a **new linked issue**; `toStatus:"reopen"` on a resolved/closed issue creates a new Issue linked to the original, rather than reactivating it.

#### `PATCH /issues/{id}/assignment`
- **Purpose:** Assign/unassign an Issue to an Authority (FR-24).
- **Auth:** Session. **Authorization:** Authority (self-assign within scope) / Admin (any).
- **Body:** `{ "assigneeId":"usr_auth_1" }` or `{ "assigneeId": null }`
- **Response `200`.**
- **Errors:** `422` (assignee out of category scope), `FORBIDDEN`, standard.

#### `PATCH /issues/{id}/severity`
- **Purpose:** **Authority severity override** (FR-20). Never overwrites computed value; both retained with actor + mandatory reason (BR-20/21, C-8).
- **Auth:** Session. **Authorization:** Authority (scope) / Admin only.
- **Body:** `{ "severity":"medium", "reason":"verified minor, no traffic risk" }`
- **Validation:** `reason` mandatory (`422` if absent); severity ∈ {Critical, High, Medium, Low} (Q2 RESOLVED).
- **Response `200`:** issue with `severity.overridden` populated and `severity.computed` unchanged.
- **Errors:** `422`, `FORBIDDEN`, standard.

#### `POST /issues/{id}/merge`
- **Purpose:** Merge this issue's cluster with another mis-clustered duplicate (FR-25).
- **Auth:** Session. **Authorization:** Authority (scope) / Admin.
- **Body:** `{ "mergeWithIssueId":"iss_789", "reason":"same pothole" }`
- **Response `200`:** surviving issue.
- **Errors:** `409` (illegal merge, e.g. closed issues), `FORBIDDEN`, standard.
- **DM-Q7 RESOLVED:** On merge, all member Reports and Confirmations re-attribute to the surviving Issue; severity recomputed as max of all members.

#### `POST /issues/{id}/split`
- **Purpose:** Split incorrectly-merged reports into a new issue (FR-25). Each side must retain ≥1 Report (BR-14/C-4).
- **Auth:** Session. **Authorization:** Authority (scope) / Admin.
- **Body:** `{ "reportIds":["rep_2","rep_5"], "reason":"different issue" }`
- **Response `201`:** new issue + updated original.
- **Errors:** `422` (would empty a side), `409`, `FORBIDDEN`, standard.
- **DM-Q7 RESOLVED:** On split, moved Reports carry their own data to the new Issue; Confirmations re-attribute accordingly.

> Issues are **not** created directly via the API — they are formed by async clustering (FR-18, Architecture §4.3). There is deliberately **no** `POST /issues`. Issues are also never hard-deleted; moderation hides content (FR-31).

---

### 6.6 Confirmations ("me-too") — `/issues/{id}/confirmations`

#### `POST /issues/{id}/confirmations`
- **Purpose:** Citizen confirms an issue affects them too (S3), feeding **distinct-reporter** corroboration (FR-16, BR-22).
- **Auth:** Session (anonymous confirmation not modeled; tied to distinct-reporter rule).
- **Authorization:** Citizen; **at most one per citizen per issue** (BR-23) → `409 ALREADY_CONFIRMED` on repeat.
- **Body:** none (or `{}`).
- **Response `201`:** `{ "issueId":"iss_456", "corroborationCount": 7 }`
- **Errors:** `409 ALREADY_CONFIRMED`, `404`, standard.

#### `DELETE /issues/{id}/confirmations/me`
- **Purpose:** Withdraw a confirmation (DM-Q5 RESOLVED: revocable).
- **Auth:** Session. **Authorization:** Confirming citizen.
- **Response `204`.**
- **Note:** Corroboration count decreases on withdrawal.

---

### 6.7 Comments — `/issues/{id}/comments`

#### `GET /issues/{id}/comments`
- **Purpose:** List comments. Public comments visible to citizens; **internal notes** only to Authority/Admin (FR-24).
- **Auth:** Session or public (Q7 RESOLVED: public). **Authorization:** visibility filter applied server-side by role.
- **Params:** pagination; `sort` default `createdAt`.
- **Response `200`:** collection with `visibility: "public"|"internal"`.

#### `POST /issues/{id}/comments`
- **Purpose:** Add a comment/public update or internal note (FR-24).
- **Auth:** Session. **Authorization:** Citizen → public only; Authority/Admin → public or internal (scope for authority).
- **Body:** `{ "body":"…", "visibility":"public" }` (citizens forced to `public`; `internal` requires authority/admin).
- **Response `201`.**
- **Errors:** `422`, `FORBIDDEN` (citizen requesting internal), standard.

#### `PATCH /issues/{id}/comments/{commentId}` · `DELETE …`
- **Purpose:** Edit own comment; delete (author own, or admin moderation FR-31).
- **Auth:** Session. **Authorization:** Author (own) / Admin.
- **Responses:** `200` / `204`.
- **Errors:** `FORBIDDEN`, `404`, standard.

---

### 6.8 Status history — `/issues/{id}/status-events`

#### `GET /issues/{id}/status-events`
- **Purpose:** Immutable transition history of the issue (§6.3, FR-32) — powers citizen tracking and time-to-resolution.
- **Auth:** Session or public (Q7 RESOLVED: public). **Authorization:** visibility filter applied server-side by role.
- **Response `200`:**
```json
{ "data":[ { "from":"triaged","to":"acknowledged","actorRole":"authority","reason":null,"at":"…" } ], "page": {…} }
```
- **Note:** append-only; **no POST/PATCH/DELETE** (C-9, BR-31).

---

### 6.9 Map & Analytics

#### `GET /map/issues`
- **Purpose:** Issues as **GeoJSON** for the map / hotspot view (FR-23). Server-side spatial aggregation at low zoom to bound payload (Architecture §5.2).
- **Auth:** Session/public (❓Q7). **Authorization:** scope/role; public subset for citizens.
- **Params:** `?bbox=minLng,minLat,maxLng,maxLat` (required), `?zoom=`, plus issue filters (`category`, `severity`, `status`). At low zoom returns aggregated cluster features; at high zoom, individual issues.
- **Response `200`:** GeoJSON `FeatureCollection` (features carry `severity`, `status`, `corroborationCount`, or cluster `count`).
- **Errors:** `VALIDATION_FAILED` (missing/oversized bbox), `FORBIDDEN`, standard.

#### `GET /analytics/summary`
- **Purpose:** Aggregate counts and operational metrics (FR-26): by category/severity/status/area, median time-to-resolution, open-vs-resolved trend.
- **Auth:** Session. **Authorization:** Authority (scope-limited aggregates) / Admin (all).
- **Params:** `?from=&to=&groupBy=category|severity|status|area&category=&bbox=`
- **Response `200`:** aggregate series/objects (read-only, derived).
- **Errors:** `VALIDATION_FAILED`, `FORBIDDEN`, standard.

---

### 6.10 Reference data (Admin-managed) — `/categories`, `/pois`, `/severity-keywords`, `/clustering-rules`

All are **admin-managed reference data** (FR-30, NFR-11, BR-34). Reads are broadly available (citizens need categories/POIs for display); writes are **Admin-only**.

#### Categories — `/categories`
- `GET /categories` — **Purpose:** taxonomy for forms/filters (§6.2). **Auth:** public/session. **Response:** `[{ "key":"roads","label":{"en":"Roads & Transport","bn":"…"},"active":true }]`.
- `POST /categories` *(Admin)* — create. **Body:** `{ "key":"…","label":{…} }`. `201`.
- `PATCH /categories/{key}` *(Admin)* — update/retire (`active:false`; historical refs retained). `200`.
- **Errors:** `FORBIDDEN`, `409` (duplicate key), `422`, standard. *(No hard delete — retire only.)*

#### Points of Interest — `/pois`
- `GET /pois` — **Auth:** session; **Params:** `?type=&bbox=&nearLng=&nearLat=&radiusM=` + pagination. Display/reference (FR-17).
- `POST /pois` *(Admin)* — **Body:** `{ "name":"…","type":"hospital","location":{"lng","lat"},"source":"osm" }`. `201`.
- `PATCH /pois/{id}` *(Admin)* — update/retire. `DELETE /pois/{id}` *(Admin)* — retire (soft). 
- **Errors:** `FORBIDDEN`, `422`, standard. *(POIs never affect severity — C-10.)*

#### Severity Keywords — `/severity-keywords` *(Admin)*
- `GET /severity-keywords` *(Authority/Admin)* — list bilingual keyword→severity mappings (FR-13a).
- `POST` / `PATCH /{id}` / `DELETE /{id}` *(Admin)* — manage fallback keywords. **Body:** `{ "term":"বন্যা","language":"bn","severity":"high","category":"water_drainage" }`.
- **Errors:** `FORBIDDEN`, `422`, standard.

#### Clustering Rules — `/clustering-rules` *(Admin)*
- `GET /clustering-rules` *(Admin)* — per-category radius/time-window (FR-18, ASSUMP-4).
- `POST` / `PATCH /{id}` *(Admin)* — **Body:** `{ "category":"roads","radiusM":50,"timeWindowHours":72 }`. Changes affect **future** clustering only (BR: Clustering Rule lifecycle).
- **Errors:** `FORBIDDEN`, `422`, standard.

#### City Boundary — `/meta/city-boundary`
- `GET /meta/city-boundary` — served-city polygon for client-side out-of-city hints (BR-35). **Auth:** public/session.
- `PUT /meta/city-boundary` *(Admin)* — set boundary. Single served city (PRD §2.2).

---

### 6.11 Notifications — `/notifications`

#### `GET /notifications`
- **Purpose:** List the current user's notifications (FR-27).
- **Auth:** Session. **Authorization:** Self only.
- **Params:** `?unread=true&type=` + pagination; default `-createdAt`.
- **Response `200`:** collection `{ "id","type","issueId","body","channel","read":false,"createdAt" }`.

#### `PATCH /notifications/{id}` · `POST /notifications/read-all`
- **Purpose:** Mark read (`{ "read": true }`) / mark all read.
- **Auth:** Session. **Authorization:** Self. **Response:** `200` / `204`.

#### `GET /notifications/stream`
- **Purpose:** **In-app real-time delivery via Server-Sent Events** (ASSUMP-3; websockets deemed unnecessary for the 1-min SLA). Client subscribes; server pushes new-notification events.
- **Auth:** Session. **Authorization:** Self.
- **Response:** `text/event-stream` (SSE); events reference notification IDs to fetch/patch.
- **Note:** This is the only "event endpoint." **Outbound webhooks to external/government systems are explicitly NOT provided** — external integration is a PRD §2.2 non-goal; adding them would introduce an unauthorized feature.

---

### 6.12 Notification Preferences — `/notification-preferences`

#### `GET /notification-preferences` · `PATCH /notification-preferences`
- **Purpose:** Read/update per-channel preferences and opt-outs (FR-28).
- **Auth:** Session. **Authorization:** Self.
- **Body (PATCH):** `{ "inApp": true, "email": true, "sms": false }`
  - Note: SMS is **reserved for High-severity** server-side regardless of preference (BR-30, RISK-9); disabling SMS is honored, enabling it does not bypass the severity gate.
- **Response `200`.**
- **Errors:** `VALIDATION_FAILED`, standard.

---

### 6.13 Moderation — under `/issues`, `/reports`, `/media`, `/comments`

Moderation (FR-31) is expressed as **actions on existing resources**, not a separate resource, keeping REST consistent. Each is Admin-only (Authority limited per §4.2) and **audited** (FR-32).

#### `POST /reports/{id}/moderation` · `POST /issues/{id}/moderation` · `POST /media/{id}/moderation` · `POST /issues/{id}/comments/{commentId}/moderation`
- **Purpose:** Hide/remove privacy-violating/abusive/illegal content (FR-31), reason logged.
- **Auth:** Session. **Authorization:** Admin (Authority limited).
- **Body:** `{ "action":"hide"|"remove", "reason":"exposes bystander faces" }`
- **Response `200`.** Subsequent public GETs return `410 Gone`.
- **Errors:** `FORBIDDEN`, `422` (missing reason), standard.

---

### 6.14 Audit — `/audit-events`

#### `GET /audit-events`
- **Purpose:** Query the append-only integrity log (FR-32, NFR-10).
- **Auth:** Session. **Authorization:** **Admin** → all; **Authority** → **own actions only** (§4.2). Citizens: none.
- **Params:** `?actorId=&action=&targetType=&targetId=&from=&to=` + pagination.
- **Response `200`:** `{ "data":[ { "actorId","action","targetType","targetId","before","after","at" } ], "page": {…} }`
- **Note:** read-only; no write/modify endpoints (C-9, BR-31).

---

### 6.15 Exports — `/exports`

#### `POST /exports`
- **Purpose:** Generate a CSV/GeoJSON extract of reports/issues for the technical-report deliverable (NFR-12).
- **Auth:** Session. **Authorization:** Authority (scope-limited) / Admin.
- **Body:** `{ "resource":"issues"|"reports", "format":"csv"|"geojson", "filters": { "from":"…","category":"…","bbox":"…" } }`
- **Response `202`:** `{ "exportId":"exp_1","state":"processing" }` (async for large sets).
- **Errors:** `VALIDATION_FAILED`, `FORBIDDEN`, standard.

#### `GET /exports/{id}`
- **Purpose:** Poll export status / obtain download link.
- **Auth/Authorization:** creator / Admin.
- **Response `200`:** `{ "state":"ready","downloadUrl":"…","expiresAt":"…" }`

---

### 6.16 System — `/health`, `/meta`

- `GET /health` — **Purpose:** liveness/readiness incl. dependency degradation flags (LLM/geocoder up/fallback — NFR-4/9). **Auth:** none (or internal). **Response `200`/`503`.**
- `GET /meta/enums` — **Purpose:** machine-readable enums (severities, statuses, categories, notification types) so clients stay in sync as the taxonomy evolves. Severity enum is now **Critical / High / Medium / Low** (Q2 RESOLVED). **Auth:** public/session.

---

## 7. Consolidated Authorization Matrix (by operation)

| Operation | Anonymous | Citizen | Authority (in scope) | Admin |
|-----------|:--:|:--:|:--:|:--:|
| Register / login / verify | ✅ | ✅ | ✅ | ✅ |
| Submit report | ❌ (Q4 RESOLVED: login required) | ✅ | ✅ | ✅ |
| Read report/issue (public) | ✅ (Q7 RESOLVED: public) | ✅ (own+public) | ✅ (scope) | ✅ |
| Track own reports / notifications | — | ✅ | ✅ | ✅ |
| Confirm ("me-too") | — | ✅ (1×, BR-23) | ✅ | ✅ |
| Comment (public) | — | ✅ | ✅ | ✅ |
| Comment (internal) | — | — | ✅ | ✅ |
| Change issue status / assign | — | — | ✅ (BR-15/26) | ✅ |
| Override severity (+reason) | — | — | ✅ (BR-20) | ✅ |
| Merge / split | — | — | ✅ | ✅ |
| Re-categorize report | — | — | ✅ | ✅ |
| Manage reference data | — | — | — | ✅ |
| Provision authorities | — | — | — | ✅ |
| Moderate content | — | — | (limited) | ✅ |
| Read audit log | — | — | own only | all |
| Exports | — | — | ✅ (scope) | ✅ |

---

## 8. Security Considerations (API-level)

- **Transport:** HTTPS/TLS enforced; HSTS (NFR-5).
- **Session cookies:** `Secure`, `HttpOnly`, `SameSite`; CSRF token on state-changing requests (§2).
- **AuthZ everywhere:** every mutating and sensitive-read endpoint checks role + scope server-side (FR-3, BR-27); scope leakage (Authority reading out-of-scope issues) returns `403`/`404` (avoid existence leaks).
- **No enumeration:** login, registration, and password-reset responses are generic; `404` is used to hide resources the caller may not see (Q7 RESOLVED: public map/list; exact-location exposure accepted).
- **Input validation** at the boundary for every body/param (NFR-5); reject unknown fields where strictness matters (config endpoints).
- **Object references** are opaque IDs; no IDOR via guessable identifiers.
- **File upload hardening:** type/size limits (413/415), server-side image processing, **EXIF/GPS stripped** (P3), content moderation path (FR-31).
- **PII minimization:** report text sent to the LLM is minimized (P7, A11); the API never returns another user's contact info.
- **Rate limiting & cost caps:** §4.5; LLM-triggering paths degrade to fallback rather than fail (FR-13a, NFR-13).
- **Auditability:** privileged actions (status, override, assignment, provisioning, moderation, reference-data) are all audited (FR-32).
- **Immutable histories:** status-events and audit-events expose no write endpoints (C-9).

---

## 9. Self-Review (Principal Architect)

*(Consistency, missing/redundant endpoints, security, and REST improvements. Advisory; no new features introduced — gaps that would require new business rules are surfaced as open questions, not invented.)*

### Consistency
- **Uniform envelope, pagination, error model, and auth** applied across all resources. ✅
- **Sub-resource nesting** (`/issues/{id}/comments|confirmations|status-events|reports`) is consistent and reflects Domain ownership. ✅
- **Actions modeled as PATCH on sub-state** (`/status`, `/assignment`, `/severity`) rather than ad-hoc verbs — REST-consistent and keeps the audit/notification triggers coherent. ✅

### Missing endpoints — considered and resolved
- **No `POST /issues`** — correct: Issues arise only from async clustering (FR-18). Documented deliberately, not an omission.
- **Report → Issue linkage** is exposed via `GET /reports/{id}` (`issueId`) and `GET /issues/{id}/reports` — no separate join endpoint needed.
- **Bulk status update** (FR-25 "bulk") — *gap:* the queue supports bulk operations in the PRD. Recommend a future `POST /issues/bulk-status` (additive, `v1`-safe) rather than inventing now; flagged so it isn't forgotten.
- **`GET /meta/enums`** added so clients absorb enum evolution (Critical severity now included — Q2 RESOLVED) without a breaking release.

### Redundant endpoints — checked
- Considered separate top-level `/confirmations` and `/comments` collections; **rejected** as redundant with the nested forms. Kept nested-only for a single clear ownership path.
- Considered `PUT` variants; **rejected** in favor of `PATCH` (partial, safer) — no full-replacement use case exists.

### Security concerns raised
- **Login required (Q4 RESOLVED)** — `POST /reports`/`POST /media` require a Citizen session. IP rate-limiting still applies for unauthenticated login/register attempts.
- **Public read (Q7 RESOLVED)** — exact coordinates are publicly visible. Privacy risk (P1) is accepted; EXIF is still stripped (P3) and PII in response bodies is minimized.
- **SMS gate (BR-30)** is enforced server-side irrespective of preference — prevents cost/abuse bypass (RISK-9). ✅
- **Export links** must be short-lived, signed URLs (`expiresAt`) to avoid data leakage of bulk PII/location.

### Open items — all resolved
- **DM-Q5 RESOLVED** — confirmations are revocable; `DELETE …/confirmations/me` enabled; count can decrease.
- **DM-Q7 RESOLVED** — merge re-attributes all Reports/Confirmations to surviving Issue; split moves Reports/Confirmations to new Issue.
- **Q8 / DM-Q8 RESOLVED** — Resolved = authority self-attestation; reopen = new linked Issue.
- **Q2 RESOLVED** — severity enum is Critical / High / Medium / Low.
- **Q4 RESOLVED** — login required for all writes.
- **Q7 RESOLVED** — public map and list.

### REST design improvements applied / recommended
- **Cursor pagination** over offset for the concurrently-mutated queue (§4.4). ✅ applied.
- **`202 Accepted` + polling / SSE** for async triage and exports rather than blocking — matches NFR-3. ✅ applied.
- **Capability discovery** via `/meta/enums` and `/health` degradation flags improves client resilience. ✅ applied.
- **Recommend (future, additive):** conditional requests (`ETag`/`If-Match`) on Issue mutations to prevent lost updates when two authorities edit concurrently (Architecture §11 optimistic locking) — introduce in `v1` as an optional header; not required for prototype.

### Open items forwarded (not invented here)
- **DM-Q5** — confirmation revocation → determines `DELETE …/confirmations/me` behavior and count monotonicity.
- **DM-Q7** — merge/split re-attribution → precise effect on member reports/confirmations.
- **DM-Q8 / PRD ❓Q8** — reopen semantics and the definition of "Resolved" → gating rule on `PATCH /issues/{id}/status`.
- **PRD ❓Q2** — Critical severity band → enum + potential `v2` client impact.
- **PRD ❓Q4 / ❓Q7** — anonymous reporting & public visibility → auth on report submission and public reads.

---

## 10. Traceability — Source → API

| Source | Endpoints |
|--------|-----------|
| FR-1/4 auth | §6.1 `/auth/*` |
| FR-2/3 authority provisioning, RBAC | §6.2 `/users/authorities`, matrix §7 |
| FR-5/6/8/11 report intake & correction | §6.3 `/reports*` |
| FR-7/P3 media & EXIF | §6.4 `/media*` |
| FR-14/15/20 severity & override | §6.5 `/issues/{id}/severity` |
| FR-16/S3 corroboration | §6.6 confirmations |
| FR-18/25 clustering, merge/split | §6.5 merge/split; no POST /issues |
| FR-22 queue | §6.5 `GET /issues` |
| FR-23 map/hotspots | §6.9 `/map/issues` |
| FR-24 lifecycle, comments | §6.5 status/assignment, §6.7 comments |
| FR-26 analytics | §6.9 `/analytics/summary` |
| FR-27/28/29 notifications | §6.11/§6.12 |
| FR-30/NFR-11 reference data | §6.10 |
| FR-31 moderation | §6.13 |
| FR-32/NFR-10 audit | §6.8 status-events, §6.14 audit |
| NFR-12 export | §6.15 |
| NFR-3/4 async & degradation | `202` + SSE, `/health` |
| NFR-13/FR-33 rate/cost | §4.5 |

---

*End of `docs/04-api-specification.md` (v1.0). Contracts derive solely from the approved PRD, Architecture, and Domain Model; no new business rules were introduced. Endpoints touching unresolved questions (❓Q2/Q4/Q7/Q8, DM-Q5/Q7/Q8) reference them explicitly and must be finalized before implementation. Next: `05-project-plan.md`.*
