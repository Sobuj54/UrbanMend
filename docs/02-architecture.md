# UrbanMend — Backend Architecture / Software Design Document (SDD)

> How the UrbanMend backend is structured to satisfy the requirements in `docs/01-prd.md`.

| | |
|---|---|
| **Document** | `docs/02-architecture.md` |
| **Version** | 1.2 (ASSUMP-1 resolved: Django + DRF committed — see ADR-001) |
| **Status** | Planning phase — pending stakeholder sign-off |
| **Author role** | Principal Backend Architect |
| **Date** | 2026-08-03 |
| **Source of truth** | `docs/01-prd.md` (PRD v1.1, approved) |
| **Scope of this doc** | **Backend architecture only.** Frontend/UI is treated as a client of the API. |
| **Downstream docs** | `03-data-model.md` (schema detail), `04-api-specification.md` (endpoint contracts), `05-project-plan.md`, `07-adr-001-app-framework.md` (framework decision) |

### Ground rules for this document
- The **PRD is the source of truth.** This document introduces **no new features** and changes **no requirements**. Every component traces to a PRD `FR-x` / `NFR-x`.
- Where the PRD leaves something genuinely open, it is recorded in **§13 Assumptions**, not guessed.
- **No implementation code.** Structures, flows, and contracts are described, not coded.
- Detailed **schema** belongs to `03-data-model.md`; detailed **endpoints** belong to `04-api-specification.md`. This document defines the *shape* and *boundaries*, and references those docs to avoid duplication/drift.

---

## 1. Architectural Goals & Drivers

Distilled from the PRD, these are the forces the architecture must satisfy (each cites its origin):

| Driver | Source | Architectural consequence |
|--------|--------|---------------------------|
| First-class geospatial (radius, nearest-POI, clustering, map density) | NFR-1, FR-16/17/18/23 | Spatial datastore (PostGIS); spatial indexes are core, not optional. |
| Fast submission, slow AI | G1, NFR-3, FR-10 | Write path is synchronous & cheap; classification/clustering are **asynchronous** background work. |
| Never hard-depend on the external LLM | NFR-4, FR-13a | Classification behind an interface with a **deterministic keyword fallback**; graceful degradation. |
| Report vs Issue separation | §6.1, FR-18 | Two aggregates; severity/status/assignment on **Issue**; corroboration derived from member Reports. |
| Explainable, auditable triage | FR-15, FR-20, FR-32 | Severity carries its rationale; every integrity action is written to an append-only audit log. |
| RBAC on every server action | FR-3 | Authorization enforced in the service layer, not just the UI. |
| Notifications within 1 minute | FR-27, G6 | Event-driven, asynchronous notification dispatch with channel abstraction. |
| Cost/abuse control for the LLM | NFR-13, RISK-3/12 | Rate limiting, token caps, response caching, spend ceiling, fallback on breach. |
| Modest scale, tight timeline, small team | A7, A10, RISK-11 | Favor simplicity: a **modular monolith**, not distributed services. |
| Privacy of text & images | §9, P3/P7, A8/A11 | EXIF stripping, PII-minimized prompts, moderation hooks, object storage separation. |

---

## 2. Architecture Overview

### 2.1 Style decision — Modular Monolith

**Decision:** A single deployable backend application, internally decomposed into strictly-bounded modules, backed by one primary database, one cache/queue, and one object store. Background work runs in a **worker process** of the same codebase.

**Why (trade-off analysis):**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Modular monolith** (chosen) | Simple to build/deploy/debug; one transaction boundary; fast for a small team; no network failure modes between components; refactor-friendly | Must enforce module boundaries by discipline; scales as one unit | ✅ Fits A7 scale, A10 timeline, RISK-11 |
| Microservices | Independent scaling/deploy; strong isolation | Distributed transactions, network failure surface, ops overhead, more infra — massive overkill here | ❌ Violates RISK-8/11 |
| Serverless functions | No server mgmt; scales to zero | Cold starts hurt the 1-min notification SLA; awkward with PostGIS connection pools & long LLM calls; harder to keep audit/transaction integrity | ❌ Poor fit |

The monolith is decomposed so a future extraction (e.g. pulling the classification worker into its own service) is mechanical, not a rewrite. That preserves the option without paying for it now.

### 2.2 Runtime topology (logical)

```
                          ┌──────────────────────────────┐
   Mobile-responsive      │        API process(es)        │
   web client  ──HTTPS──▶ │  (stateless request handling) │
   (out of scope)         │  Auth · RBAC · Reports · Issues│
                          │  Dashboard · Admin · Export    │
                          └───────┬───────────┬───────────┘
                                  │           │ enqueue jobs / read-write
                    read/write    │           ▼
                 ┌────────────────▼─────┐   ┌─────────────────────┐
                 │ PostgreSQL + PostGIS │   │   Redis (queue +     │
                 │  (system of record)  │   │   cache + rate limit)│
                 └────────────────▲─────┘   └─────────┬───────────┘
                                  │ read-write          │ consume jobs
                          ┌───────┴─────────────────────▼──────────┐
                          │            Worker process(es)           │
                          │  Triage pipeline · Media processing ·   │
                          │  Notification dispatch · (scheduled)    │
                          └───┬───────────────┬───────────────┬─────┘
                              │               │               │
                         ┌────▼─────┐   ┌──────▼──────┐  ┌─────▼───────┐
                         │ LLM API  │   │ Object store│  │ Email / SMS │
                         │ provider │   │ (S3-compat) │  │  providers  │
                         └──────────┘   └─────────────┘  └─────────────┘
```

- **API process** — stateless HTTP handlers; can run N replicas behind a load balancer if needed. Does cheap synchronous work only.
- **Worker process** — same codebase, consumes jobs from Redis. Does all slow/external work (LLM, image processing, notification sending, clustering).
- **PostgreSQL + PostGIS** — the single system of record and spatial engine.
- **Redis** — job queue, cache (reference data, LLM response cache), and rate-limit counters.
- **Object store** — photos and generated thumbnails (never in the DB).
- **External providers** — LLM, email, SMS — each reached only through an internal adapter.

### 2.3 Recommended technology stack

Per PRD §16.5, the app *language/framework* choice was delegated to this document. Committed choices are forced by requirements; the framework was a recommendation to confirm — **now resolved as Python + Django + DRF in `docs/07-adr-001-app-framework.md` (ADR-001, Accepted).**

| Layer | Recommendation | Status | Rationale |
|-------|----------------|--------|-----------|
| Primary datastore | **PostgreSQL + PostGIS** | **Committed** (NFR-1) | Only mainstream store that gives ACID + first-class geospatial in one engine; avoids a second specialized DB. |
| Queue / cache | **Redis** (+ a durable job library) | **Committed** | Simplest reliable async at this scale; doubles as cache and rate-limit store. |
| Object storage | **S3-compatible** (cloud S3, or self-hosted MinIO) | **Committed** | Keeps large binaries out of the DB; presign/stream support. |
| App framework | **Python + Django + DRF** | **Committed (ADR-001)** | Server-validated sessions, RBAC, admin-driven reference data, and migrations are built in; GeoDjango is the most mature spatial ORM (NFR-1). Full rationale in ADR-001. |
| Async worker / queue | **Celery (Redis broker) + Celery beat** | **Committed (ADR-001)** | Same codebase, worker process pattern (§2.2); `transaction.on_commit` for task enqueue, outbox relay on beat. |
| Geospatial / mapping | **GeoDjango + `djangorestframework-gis`** | **Committed (ADR-001)** | `geography`-typed columns, GiST indexes, `ST_DWithin`/KNN/grid aggregation via the ORM — NFR-1 is core, not optional. |
| Object-store client | **`django-storages`** | **Committed (ADR-001)** | S3-compatible uploads/streaming from the app. |
| App server | **ASGI (uvicorn)** | **Committed (ADR-001)** | Keeps the SSE notification stream (ASSUMP-3) off sync worker threads. |
| Migrations | **Django migrations** | **Committed (ADR-001)** | Versioned, reproducible schema (feeds doc 03); first migration enables the PostGIS extension. |

> The module boundaries remain framework-agnostic (ADR-001 §4), but the framework is no
> longer open — all framework-specific detail is confined to the API and persistence layers.

---

### 2.4 Module → Django app mapping (ADR-001)

Each §3 module becomes one Django app; the modular-monolith boundaries of §2.1 are enforced by
codebase layout and service interfaces, not by the framework. This subsection records the
framework-level decisions only — detailed schema stays in doc 03, endpoints in doc 04.

| §3 module | Django app |
|-----------|-----------|
| Identity & Access | `identity` |
| Reporting | `reporting` |
| Media | `media` |
| Classification | `classification` |
| Issues & Clustering | `issues` |
| Geospatial | `geo` |
| Dashboard & Query | served by `issues` / `geo` selectors |
| Notifications | `notifications` |
| Administration & Moderation | `moderation` |
| Audit & Integrity | `audit` |
| Export | `export` |
| Platform (cross-cutting) | `platform` |

**Framework-level decisions:**

- **Layering (§3.1).** DRF views stay thin; business rules, RBAC checks, and transactions live
  in `services.py` (writes) and `selectors.py` (reads) per app. DRF permission classes are
  defence-in-depth, **not** the enforcement point — FR-3 requires service-layer authorization.
- **Custom user model** is declared before the first migration (irreversible afterwards).
- **RBAC** is an explicit `role` field plus an authority↔category scope relation evaluated in
  the service layer. `django.contrib.auth` Groups/Permissions are **not** used for this: they
  cannot express the category/department scoping in BR-26.
- **Sessions (§8)** use Django's session framework on the `cached_db` backend — the opaque
  cookie-borne token this document already specifies; revocation deletes session rows.
- **CSRF** is carried by DRF `SessionAuthentication` for state-changing requests (API §2).
- **Task enqueue** always fires via `transaction.on_commit`, so a worker can never observe an
  uncommitted Report (§4.1).
- **Outbox (§7.1)** is a real table written in the state-change transaction, polled by a Celery
  beat relay using row-level skip-locked reads. An in-process commit hook alone cannot survive
  the crash this pattern exists to guard against.
- **Clustering find-or-create (§4.3)** takes a transaction-scoped Postgres advisory lock keyed
  on geohash-cell + category inside an atomic block.
- **Reference data and moderation (FR-30/FR-31)** are surfaced through Django admin.
- **SSE (§7.2, ASSUMP-3)** is served from the ASGI stack; polling remains the sanctioned
  fallback.

---

## 3. Module Decomposition (Bounded Contexts)

The monolith is divided into modules with explicit responsibilities and dependencies. Modules communicate in-process via service interfaces and via **domain events** (§7) — never by reaching into each other's tables.

| Module | Responsibility | Key PRD refs |
|--------|----------------|--------------|
| **Identity & Access** | Registration, login, phone/email verification (OTP), sessions, password reset, RBAC enforcement, authority provisioning, optional 2FA | FR-1, FR-2, FR-3, FR-4 |
| **Reporting** | Report intake, validation, media association, submission idempotency, "me-too"/confirm, comments | FR-5, FR-8, FR-9, FR-11, S3 |
| **Media** | Upload handling, server-side compression, thumbnailing, EXIF stripping, storage keys | FR-7, P3 |
| **Classification** | LLM adapter + prompt construction + response validation; keyword fallback engine; caching; cost/rate control | FR-10, FR-12, FR-13, FR-13a, NFR-13, S1 |
| **Issues & Clustering** | Report→Issue find-or-create clustering, Issue-level severity resolution, lifecycle state machine, merge/split, assignment, overrides | FR-14, FR-15, FR-18, FR-19, FR-20, FR-24, FR-25, §6.3 |
| **Geospatial** | Spatial queries (radius, nearest-POI, density), reverse geocoding integration, POI reference data | FR-6, FR-16, FR-17, FR-23, NFR-1 |
| **Dashboard & Query** | Severity-ranked queue reads, filters, map/hotspot data, analytics aggregates | FR-22, FR-23, FR-26 |
| **Notifications** | Event-driven dispatch across in-app/email/SMS, preferences, debounce | FR-27, FR-28, FR-29 |
| **Administration & Moderation** | Reference-data management (POIs, severity keyword lists), content moderation, account verification tools | FR-30, FR-31 |
| **Audit & Integrity** | Append-only audit log writes and queries | FR-32 |
| **Export** | CSV / GeoJSON extracts for the technical report | NFR-12 |
| **Platform (cross-cutting)** | Config, logging, metrics, error handling, rate limiting, security middleware | NFR-5, NFR-9, NFR-13 |

### 3.1 Layering within each module

```
API/Controller layer   → HTTP concerns: routing, (de)serialization, auth guard, validation
Service/Domain layer   → business rules, RBAC checks, transactions, emits domain events
Data-access layer      → repositories over PostGIS; no business logic
Integration/adapters   → LLM, email, SMS, object store, geocoder (all behind interfaces)
```

**Authorization is enforced in the service layer** (FR-3) so it cannot be bypassed by any caller, not merely hidden in the UI.

---

## 4. The Core Flow — Report Submission & Triage

This is the system's spine and the flow most requirements touch. It is deliberately split into a **fast synchronous write path** and a **slow asynchronous triage path** (driver: NFR-3).

### 4.1 Sequence

```
CITIZEN                API PROCESS                      DB / QUEUE                 WORKER
  │  submit report ─────▶ validate (FR-5), authz (FR-3)
  │                       de-dupe via idempotency key
  │                       persist Report                ─▶ INSERT Report (status=SUBMITTED,
  │                                                          classification=PENDING)
  │                       persist media refs            ─▶ media rows (state=UPLOADED)
  │                       enqueue TriageJob(reportId)    ─▶ Redis queue
  │  ◀── 201 + reportId ──┘  (returns in well under NFR-2 budget)
  │                                                                     ┌── consume TriageJob
  │                                                                     │ 1. Classify (Classification module)
  │                                                                     │    → LLM adapter; on fail/timeout/
  │                                                                     │      over-budget → keyword fallback (FR-13a)
  │                                                                     │    → {category, severity, confidence, source}
  │                                                                     │    → validate/coerce to allowed sets
  │                                                                     │ 2. Cluster (Issues module)  ← needs category
  │                                                                     │    → spatial+category+time find-or-create
  │                                                                     │      (locked, see §4.3)
  │                                                                     │ 3. Attach Report→Issue; recompute Issue
  │                                                                     │    severity (highest-wins, §4.4);
  │                                                                     │    update corroboration count (FR-16)
  │                                                                     │ 4. Compute proximity context (nearest
  │                                                                     │    POIs, FR-17) — display-only
  │                                                                     │ 5. Issue.status → TRIAGED
  │                                                                     │ 6. Emit ReportTriaged / IssueUpdated events
  │                                                                     └── (events → Notifications, Audit)
  │  ◀ status visible as "processing" until step 5 completes (NFR-3) ─────────────
```

**Media processing** (compression, thumbnail, EXIF strip — FR-7/P3) runs as its own job so a large image never blocks triage; the Report is usable before its thumbnail is ready.

### 4.2 Why classification precedes clustering
Clustering (FR-18) matches on **category + proximity + time window**. Category is produced by classification, which is asynchronous. Therefore classification must complete first *within the same triage job*. Attempting to cluster on location alone before category is known would mis-merge distinct issues at the same coordinates (PRD edge case: "pothole *and* broken light at same spot").

### 4.3 Clustering as a concurrency-safe find-or-create
**Problem:** two reports of the same real-world issue arriving near-simultaneously could each create a separate Issue (a lost-update / duplicate-Issue race).

**Design:** the find-or-create runs inside a single DB transaction guarded by a **lock keyed on a coarse spatial+category bucket** (e.g. a geohash cell + category), using an advisory lock or `SERIALIZABLE` isolation with retry. Within the lock:
1. `ST_DWithin` query for an **open** Issue of the same category within the configured radius and time window.
2. If found → attach; if not → create new Issue.

Radius and time-window are **configurable defaults per category** (ASSUMP-4), managed as reference data — not hard-coded. Conservative defaults are preferred (under-merge is safer than over-merge; authorities can merge later via FR-25).

### 4.4 Issue-level severity resolution
Per PRD edge case ("conflicting severities across a cluster → highest wins, shown with rationale"): when a Report joins an Issue, the Issue severity is `max(member severities)`. The Issue records **which report/phrases drove the current severity** (FR-15). An authority override (FR-20) supersedes the computed severity and is stored alongside the original with actor + reason (never overwriting it).

---

## 5. Read Paths

### 5.1 Authority work queue (FR-22)
- Query **Issues** (not Reports), scoped to the authority's department(s) (FR-2 scoping, FR-3 authz).
- Default sort: **severity DESC, then age** (FR-19). No numeric score exists (PRD §5.4).
- Filters: category, severity, status, area (spatial bbox), date.
- Each row carries derived **corroboration count** (FR-16) and **nearest-POI context** (FR-17), both display-only.
- Pagination mandatory (NFR-2).

### 5.2 Map / hotspots (FR-23)
- Returns Issues as **GeoJSON** for the map, filterable by the same dimensions.
- Density/heat visualization is driven by server-side spatial aggregation (grid or clustering) so payloads stay small at zoom-out; the client renders. This is a read concern only — no scoring.

### 5.3 Analytics (FR-26)
- Aggregate reads: counts by category/severity/status/area, median time-to-resolution, open-vs-resolved trends. Served from the same store via aggregate queries at prototype scale; a read replica or materialized views are a *future* optimization (§14), not needed now (A7).

---

## 6. Classification Subsystem (LLM + Fallback)

Isolated behind a single **`ClassificationService` interface** (S1) so the provider (Q9 RESOLVED: provider deferred to implementation; no-training-data policy locked; adapter is provider-agnostic) is swappable without touching callers.

```
ClassificationService.classify(text, lang?, imageRef?) → {category, severity, confidence, source}
        │
        ├─▶ LLM Adapter (provider-specific)
        │     • builds a PII-minimized prompt (P7) constraining outputs to the
        │       allowed category taxonomy (§6.2) and severity set (High/Med/Low)
        │     • timeout + bounded retry with backoff
        │     • validates & coerces response to allowed values (edge case handling)
        │     • records latency/cost metrics (NFR-9)
        │
        └─▶ Keyword Fallback Engine (deterministic, FR-13a)
              • bilingual keyword lists (Bangla/English), admin-managed (FR-30)
              • used when: LLM unavailable / times out / over rate or spend cap (NFR-13)
```

**Cost & abuse controls (NFR-13, RISK-3/12):** identical-text response cache (Redis), per-user/global rate limits, per-request token cap, and a spend ceiling; on breach the request degrades to the fallback engine rather than failing. When the fallback is used, no report text is sent externally at all (a privacy bonus, RISK-12).

**`source` field** (`llm` vs `fallback`) is persisted so the technical report (NFR-12, KPIs) can measure fallback-invocation rate and compare accuracy.

---

## 7. Eventing, Consistency & the Notification Path

### 7.1 Domain events & the dual-write problem
State changes (Report triaged, Issue status changed, severity overridden) must reliably trigger notifications (FR-27, ≤1 min) and audit writes (FR-32) **without** losing events if the process crashes between "write DB" and "enqueue job."

**Design — transactional outbox:** domain events are written to an `outbox` table **in the same DB transaction** as the state change. A relay (worker) reads unprocessed outbox rows and dispatches them to the queue, marking them done. This guarantees *at-least-once* delivery and keeps the DB the single source of truth. Consumers (Notifications, Audit) are made **idempotent** to tolerate duplicates.

> *Trade-off:* a plain "write then enqueue" is simpler but can silently drop notifications on crash — unacceptable given the 1-minute SLA is a stated goal. The outbox is a small, well-understood pattern and is the correct senior choice; it is also the seam along which a service could later be extracted.

### 7.2 Notification dispatch (FR-27/28/29)
- Consumes notification events; resolves recipient channels from preferences (FR-28).
- **In-app** always; **email** per preference; **SMS reserved for High-severity** (RISK-9 cost control).
- **Debounce** rapid successive status changes on one issue (PRD edge case) so citizens aren't spammed.
- Never sends to an unverified/invalid channel and never leaks a report's existence to a wrong recipient (edge case).
- Delivery outcome recorded for the notification-health KPI (NFR-9).
- In-app delivery to the client uses **polling or Server-Sent Events** (ASSUMP-3) — websockets are unnecessary for a 1-minute SLA.

### 7.3 Audit (FR-32)
Append-only `audit_log` capturing actor, action, timestamp, and before/after for: auth events, role grants, status transitions, **severity overrides**, moderation, and reference-data changes. Written by the service layer (which has the actor/context) via the same event mechanism; the table is insert-only (no updates/deletes) to preserve integrity (NFR-10).

---

## 8. Identity, Access & Session Design

- **Registration/verification (FR-1):** email or phone; phone via OTP, email via link/code. Passwords hashed with a modern adaptive algorithm (Argon2/bcrypt) (NFR-5).
- **Authority provisioning (FR-2):** authority role is granted **only** by an admin action, itself audited (FR-32). Authorities are scoped to one or more departments/categories; that scope constrains their queue (FR-22) and permissions.
- **Sessions:** **server-validated sessions** (opaque token in an httpOnly cookie) recommended over stateless JWTs, because the app needs reliable **immediate revocation** (moderation, deprovisioning) and has no cross-service token-sharing need. *Trade-off:* JWTs scale statelessly but are hard to revoke; at this scale, revocation clarity wins. **Committed:** Django's session framework (`cached_db` backend) is exactly this design — see ADR-001 §2.
- **RBAC (FR-3):** a central authorization component evaluates the §4.2 role/permission matrix in the service layer on every mutating and sensitive read action. Department scoping is part of the check for authorities.
- **Abuse baseline (FR-4, FR-33):** rate-limited login with backoff/lockout; optional 2FA for authority/admin; submission rate limits and Sybil-resistance heuristics feed the honest-corroboration rule (T1–T3).

---

## 9. Geospatial Design (NFR-1)

- **Storage:** locations as PostGIS `geography(Point, 4326)`; POIs likewise. GiST spatial indexes on all queried geometry columns.
- **Queries:**
  - Radius / "near landmark" and clustering: `ST_DWithin` (index-assisted).
  - Nearest POIs (FR-17 context): KNN (`<->`) ordered by distance.
  - Map density (FR-23): server-side spatial aggregation over a bounding box.
- **Reference data (POIs):** seeded from OpenStreetMap or a provided dataset (A5), stored as versioned reference data, admin-manageable (FR-30). Proximity is **display-only** (PRD §5.4) — it feeds context strings, never a score.
- **Reverse geocoding (FR-6):** address string derived via a geocoding adapter (behind an interface); the authoritative location is always the stored coordinate, independent of any photo EXIF (P3).
- **City boundary:** submissions outside the served city are flagged/blocked (edge case) via a point-in-polygon check against a stored city boundary.

---

## 10. Media Handling (FR-7, P3, A8)

- **Upload path:** client uploads to the API, which streams to object storage and creates a media record; a **media-processing job** then compresses, generates thumbnails, and **strips EXIF (incl. GPS) by default** (P3). (Presigned direct-to-storage upload is a viable alternative if bandwidth on the API tier becomes a concern — noted, not required at A7 scale.)
- **Validation:** MIME/type and size limits enforced server-side; corrupt/oversized/rotated images handled (orient via EXIF orientation *before* stripping) — all PRD edge cases.
- **Privacy:** originals with identifiable content are subject to moderation (FR-31); optional face/plate blurring is a PRD **COULD** (S8) and is out of scope for the core build but the media pipeline is the correct place to add it later.
- Binaries never live in PostgreSQL; the DB stores object keys and derived metadata.

---

## 11. Cross-Cutting Concerns

| Concern | Design | PRD ref |
|---------|--------|---------|
| **Security** | HTTPS everywhere; secrets (LLM keys, DB creds) via environment/secret manager, never in code; input validation at API boundary; OWASP Top 10 guards; server-side authz on every action. | NFR-5 |
| **Config as data** | Category taxonomy, POIs, severity keyword lists, clustering radius/time-window are stored/managed data, not hard-coded; admin-editable (FR-30). | NFR-11 |
| **Observability** | Structured logs with correlation IDs across API→queue→worker; metrics for report throughput, classification outcomes, **LLM latency/cost/fallback rate**, notification delivery; error tracking. | NFR-9 |
| **Rate limiting** | Redis-backed counters on auth, submission, and LLM calls (NFR-13, FR-33). | NFR-13 |
| **Localization** | Bangla/English handled end-to-end; API returns locale-neutral data, formatting deferred to client; classification prompts handle Banglish (FR-12). | NFR-8 |
| **Data lifecycle** | Documented retention; user/PII deletion (P6) anonymizes reports while retaining public issue records; export via CSV/GeoJSON (NFR-12). | §9, NFR-12 |
| **Idempotency** | Submission uses a client-supplied idempotency key (prevents double-tap duplicates); triage/notification jobs are idempotent (safe re-run). | Edge cases, §7.1 |

---

## 12. Failure Modes & Degradation

| Failure | Behavior | Guarantee |
|---------|----------|-----------|
| LLM API down / slow / over budget | Triage falls back to keyword engine (FR-13a); `source=fallback` recorded | No issue left untriaged (NFR-4) |
| Worker crash mid-triage | Job re-runs; steps are idempotent; outbox ensures events not lost | At-least-once processing |
| Crash between state write and enqueue | Outbox row already committed in the same tx; relay dispatches on recovery | No lost notifications/audit (§7.1) |
| Concurrent duplicate submissions | Spatial+category lock in find-or-create | No duplicate Issues (§4.3) |
| Object store unavailable | Report still saved; media marked pending, retried | Report never lost due to image |
| Queue backlog | Issues appear as "processing" longer; API stays responsive | Write path unaffected (NFR-2) |
| Malformed LLM output | Validated/coerced to allowed category/severity or `Other`, flagged for review | No invalid enum in DB |

Formal HA/DR is explicitly out of scope for the prototype (A10); the design degrades gracefully rather than guaranteeing uptime.

---

## 13. Assumptions (unclear items — recorded, not guessed)

| # | Assumption | Why it's an assumption | Impact if wrong |
|---|-----------|------------------------|-----------------|
| ASSUMP-1 | **RESOLVED (ADR-001):** app framework is **Python + Django + DRF**. Previously "Python/FastAPI (or TS/NestJS) — confirm". | PRD delegated stack choice (§16.5) but didn't fix a language; the team has now decided. | None — decision recorded; framework-specific detail stays confined to the API + persistence layers. |
| ASSUMP-2 | Deployment is **container-based and cloud-agnostic** (managed Postgres+PostGIS or self-hosted; S3 or MinIO). | PRD doesn't name a host. | Affects ops/deploy doc, not module design. |
| ASSUMP-3 | In-app notification delivery uses **polling or SSE**, not websockets. SSE is served from the **ASGI** stack (ADR-001) so a long-lived stream does not pin a sync worker thread; polling remains the sanctioned fallback. | 1-min SLA (FR-27) doesn't require real-time push. | Trivial to change; isolated to Notifications module. |
| ASSUMP-4 | Clustering **radius and time-window** are configurable per-category defaults, tuned later. | PRD specifies the signals (FR-18) but not exact values. | Wrong values → over/under-merge; tunable as reference data (RISK-10). |
| ASSUMP-5 | A single **reverse-geocoding provider** is available behind an adapter. | FR-6 requires an address; PRD doesn't name a source. | Swappable adapter; degrades to coordinates-only if unavailable. |
| ASSUMP-6 | A **city boundary polygon** is available to enforce the "outside city" edge case. | Implied by the edge case, not provided. | Without it, out-of-city reports can't be auto-flagged. |
| ASSUMP-7 | LLM provider/data-policy (PRD **Q9 RESOLVED**: provider deferred to implementation; no-training-data policy locked; adapter is provider-agnostic). | Open question in PRD — now resolved. | No design impact due to S1 isolation. |
| ASSUMP-8 | Single-city now, but a nullable `city` boundary in the model keeps a future city column possible (PRD §11) — **no** multi-tenant isolation is built (§2.2 non-goal). | PRD wants future-proofing without multi-tenancy. | Building tenancy now would violate scope. |

None of these introduce features; they record decisions the PRD deferred.

---

## 14. Self-Review — Weaknesses & Suggested Improvements

*(A Principal Architect reviewing their own design. These are recommendations, prioritized; none change PRD requirements — they are engineering refinements or explicitly-deferred options.)*

**Things I'd flag as the design's soft spots:**

1. **Clustering quality is the highest technical risk (RISK-10).** The whole corroboration-count feature and a clean queue depend on it, yet radius/time tuning is guesswork until real data exists. *Improvement:* ship conservative defaults, make merge/split (FR-25) genuinely easy for authorities, and log clustering decisions so they can be evaluated in the technical report. Treat clustering parameters as tunable reference data from day one (already in ASSUMP-4).

2. **`SERIALIZABLE`/advisory-lock clustering adds write-path contention** if many reports land in the same area at once (mass-event edge case, e.g. flooding). *Improvement:* keep the lock **coarse but not global** (geohash-cell + category), and confirm the mass-event scenario in load-style testing. Acceptable at A7 scale; revisit only if contention appears.

3. **The transactional outbox is the right call but adds a moving part.** *Improvement:* keep the relay simple and idempotent; if it proves heavy for a capstone, a documented fallback is a periodic sweep of unprocessed state — but I recommend keeping the outbox, as silent notification loss would undermine a graded demo.

4. **Analytics on the primary DB will contend with writes as data grows.** Fine now (A7). *Improvement (deferred, §5.3):* introduce materialized views or a read replica **only if** measured — do not build preemptively.

5. **LLM prompt drift / provider change could shift classification behavior.** *Improvement:* version the prompt and record `model+version` per classification (already specified); keep a small human-labeled evaluation set (from FR-11 corrections) to detect regressions — supports the KPI in PRD §10 without training a model.

6. **Media privacy (faces/plates) relies on manual moderation initially.** *Improvement:* the pipeline (§10) is intentionally structured so automatic blurring (S8, a PRD COULD) can be added as one more media job later, without redesign.

7. **Reverse-geocoding and city-boundary are external/data dependencies** (ASSUMP-5/6) that could be under-provisioned. *Improvement:* design both as adapters with graceful degradation (coordinates-only, skip out-of-city check) so a missing dependency weakens a feature rather than blocking submission.

**What I deliberately did *not* add** (to respect scope and avoid over-engineering): message brokers beyond Redis, a separate search engine, CQRS/event-sourcing, service mesh, or multi-region concerns. At this project's scale and timeline they would be liabilities, not strengths.

**Framework-committed observations (ADR-001):**

1. **Django admin materially reduces P8.3 effort.** Reference data (categories, POIs, severity keywords, clustering rules) and moderation (FR-30/FR-31) ship largely as admin configuration rather than hand-built CRUD — a direct, scheduled relief for the R-10 bandwidth risk.
2. **New soft spot — Django's idiom vs §3.1 layering.** Django's conventions (fat models, logic in views/serializers) push against the mandated API → service → data-access layering, and authorization must live in the service layer (FR-3). *Mitigation:* the `services.py`/`selectors.py` convention is established in §2.4 as a day-one rule, DRF permission classes are defence-in-depth only, and authorization coverage is already a T10.2 review item.

---

## 15. Traceability — PRD → Architecture

| PRD area | Architecture home |
|----------|-------------------|
| FR-1..4 Identity/RBAC | §8 Identity, Access & Session |
| FR-5,8,9,11 Reporting | §3 Reporting module, §4 core flow |
| FR-7, P3 Media | §10 Media Handling |
| FR-10,12,13,13a Classification | §6 Classification Subsystem |
| FR-14..20 Severity/Issue/lifecycle | §3 Issues module, §4.3/4.4 |
| FR-16,17,18,23, NFR-1 Geospatial | §9 Geospatial Design, §4.3 |
| FR-22,23,26 Dashboard/analytics | §5 Read Paths |
| FR-27,28,29 Notifications | §7.2 Notification path |
| FR-30,31 Admin/moderation | §3 Administration module, §11 |
| FR-32, NFR-10 Audit | §7.3 Audit |
| NFR-3,4 Async/degradation | §4 core flow, §12 Failure modes |
| NFR-5,9,11,13 Cross-cutting | §11 Cross-Cutting Concerns |
| NFR-12 Export | §3 Export module, §11 |
| Framework decision | ADR-001 (`07-adr-001-app-framework.md`) |

---

*End of `docs/02-architecture.md` (v1.2). ASSUMP-1 resolved: app framework is **Python + Django + DRF**, recorded in ADR-001 (`07-adr-001-app-framework.md`); §2.4 maps modules to Django apps. Open question Q9 resolved: LLM provider deferred to implementation; no-training-data policy locked; adapter remains provider-agnostic (ASSUMP-7). Detailed schema in `03-data-model.md`; endpoint contracts in `04-api-specification.md`. No implementation code and no new requirements were introduced.*
