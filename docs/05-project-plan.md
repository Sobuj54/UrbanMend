# UrbanMend — Backend Development Roadmap & Project Plan

> The end-to-end plan for building the UrbanMend backend, sequenced to lay foundations before dependent features and to minimize rework. Derived entirely from the four approved planning documents.

| | |
|---|---|
| **Document** | `docs/05-project-plan.md` |
| **Version** | 1.0 (Draft for review) |
| **Status** | Planning phase — pending stakeholder sign-off |
| **Author role** | Principal Software Engineer & Technical Lead |
| **Date** | 2026-07-22 |
| **Source of truth** | `01-prd.md` (v1.1) · `02-architecture.md` (v1.0) · `03-data-model.md` (v1.0) · `04-api-specification.md` (v1.0) — all approved |
| **Scope** | Backend implementation roadmap only (API process + Worker process). Client/UI is out of scope. |

### Ground rules
- The four approved docs are the **single source of truth.** This plan schedules only work that traces to an existing `FR-x` / `NFR-x` / `BR-x` / API endpoint. It introduces **no new features or requirements.**
- Where sequencing depends on an **unresolved open question** (PRD `❓Qx`, DM-`Qx`, `ASSUMP-x`), it is listed as a **blocking dependency or decision gate**, not guessed.
- **No implementation code.** Tasks describe *what* to build and *in what order*, not *how* in code.
- **Estimates are relative** (complexity + rough effort in developer-days for a **2-engineer capstone team**), not calendar-fixed. Complexity: **Low / Medium / High**.

---

## 1. Project Objectives

| # | Objective | Traces to |
|---|-----------|-----------|
| O-1 | Deliver a production-quality backend that lets citizens submit civic issue reports (text + photo + location) reliably and fast. | FR-5–8, NFR-3 |
| O-2 | Automatically classify reports (category + severity signal) via a hosted LLM, with a deterministic keyword fallback that never blocks intake. | FR-9–13a, NFR-4/13 |
| O-3 | Cluster related reports into Issues and maintain correct Report↔Issue separation. | FR-18, Domain model |
| O-4 | Give authorities a severity-ranked queue, map, lifecycle workflow, assignment, and override — all RBAC-scoped and audited. | FR-14–26, FR-32 |
| O-5 | Notify citizens of status changes within SLA using a reliable (outbox-backed) delivery path. | FR-27–29, NFR-3 |
| O-6 | Enforce security, privacy (EXIF stripping, PII minimization), rate limiting, and cost control throughout. | NFR-5/13, P1–P7 |
| O-7 | Be demonstrable, testable, documented, and deployable for the capstone deliverable. | NFR-9–12 |

**Non-objectives (carried from PRD §2.2):** native apps, real municipal/government integration, dispatch, financial integration, multi-tenant, custom-trained ML model, weighted numeric priority scoring. These are **not** scheduled.

---

## 2. Guiding Principles for Sequencing

1. **Foundations first.** Platform, data layer, auth, and CI/CD precede any feature so later work builds on stable ground (minimizes rework).
2. **Respect the domain dependency chain.** `Report intake → Classification → Clustering → Issue triage → Notifications`. Classification **must** precede clustering (clustering matches on category — Architecture §4.2).
3. **Write path before read path.** Issues must exist before the queue/map/analytics that display them.
4. **Reliability primitives before the features that need them.** Async worker + transactional outbox land before notifications and heavy triage.
5. **Cross-cutting concerns are built in, not bolted on.** AuthZ, audit, rate limiting, and validation are part of each vertical slice, not a final phase.
6. **Every phase ends demonstrable.** Each milestone produces something runnable and testable end-to-end for its slice.

---

## 3. Development Phases (Logical Order)

```
P0 Foundation ─► P1 Identity & Access ─► P2 Reporting & Media ─┐
                                                               ▼
                          P3 Classification (LLM + fallback) ──┤
                                                               ▼
                          P4 Clustering & Issues ──────────────┤
                                                               ▼
        P5 Issue Triage Workflow (status/assign/override/merge/split)
                                                               ▼
        P6 Notifications & Outbox ─► P7 Read Paths (queue/map/analytics)
                                                               ▼
        P8 Moderation, Audit & Reference Data ─► P9 Export
                                                               ▼
        P10 Hardening, Security, Performance & Deployment Readiness
```

Phases are ordered by dependency, not by perceived importance. P6 and P7 can partially overlap once P5 is stable (both consume Issues), but P7 read paths depend on P5 status data being present.

---

## 4. Milestones & Deliverables (Overview)

| Milestone | Phase | Headline deliverable | Rough effort (2 devs) |
|-----------|-------|----------------------|----------------------|
| **M0** | P0 | Running skeleton: repo, CI/CD, DB+PostGIS, Redis, object store, health check, base API/Worker processes | ~1 sprint |
| **M1** | P1 | Registration, verification, sessions, RBAC, authority provisioning | ~1 sprint |
| **M2** | P2 | Report submission (async `202`), media upload + EXIF strip, own-report tracking | ~1.5 sprints |
| **M3** | P3 | Async classification with LLM adapter + keyword fallback + cost caps | ~1 sprint |
| **M4** | P4 | Concurrency-safe clustering into Issues; corroboration; proximity context | ~1.5 sprints |
| **M5** | P5 | Full issue lifecycle: status, assignment, severity override, merge/split | ~1.5 sprints |
| **M6** | P6 | Transactional outbox + notification dispatch (in-app/email/SMS gate) + SSE | ~1 sprint |
| **M7** | P7 | Severity-ranked queue, map (GeoJSON), analytics | ~1 sprint |
| **M8** | P8 | Moderation actions, append-only audit log, admin reference-data management | ~1 sprint |
| **M9** | P9 | Export (CSV/GeoJSON) async jobs | ~0.5 sprint |
| **M10** | P10 | Security hardening, load/perf validation, deployment readiness, docs complete | ~1 sprint |

> Total ≈ 11–12 sprints of backend work. Overlap and the resolution of open questions (§10) shift this; treat as relative ordering, not a fixed calendar.

---

## 5. Phase-by-Phase Task Breakdown

Legend — **Cx** = complexity, **Dep** = depends on.

### P0 — Foundation (M0)
| Task | Cx | Dep | Notes / Traces |
|------|----|-----|----------------|
| T0.1 Monorepo/codebase layout for modular monolith (API + Worker share code) | Low | — | Architecture §2.1 |
| T0.2 Provision PostgreSQL **+ PostGIS**, Redis, S3-compatible object store (local/dev) | Med | — | Architecture §2.2, NFR-1 |
| T0.3 Config/secrets management, environment profiles (dev/stage/prod) | Med | T0.1 | NFR-5 |
| T0.4 Schema migration tooling + baseline migration framework | Med | T0.2 | Data model → schema |
| T0.5 CI pipeline (lint, test, migration check, build) | Med | T0.1 | NFR-9 |
| T0.6 Base API process (routing, `/api/v1`, error envelope, request validation harness) | Med | T0.1 | API §4.1/§5 |
| T0.7 Base Worker process (job/queue consumer skeleton on Redis) | Med | T0.2 | Architecture §2.2 |
| T0.8 `GET /health` with dependency degradation flags | Low | T0.6 | API §6.16, NFR-4 |
| T0.9 Structured logging, correlation/trace IDs, error tracking | Med | T0.6 | API §4.1 `traceId` |
| T0.10 Baseline schema for core entities (Users, Reports, Issues, Media, Categories) | High | T0.4 | Domain model |

**DoD (M0):** CI green; API and Worker both boot; migrations apply cleanly from zero; `/health` reports each dependency; a trivial round-trip request works with the standard error envelope.

### P1 — Identity & Access (M1)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T1.1 User entity + roles (Citizen/Authority/Admin) + status states | Med | T0.10 | FR-1/2, Domain |
| T1.2 Registration + email/phone verification (OTP/link) | Med | T1.1 | FR-1, `/auth/*` |
| T1.3 Server-validated sessions (cookie, `HttpOnly`/`Secure`/`SameSite`) + revocation | High | T1.1 | Arch §8, API §2 |
| T1.4 CSRF protection for state-changing requests | Med | T1.3 | API §2 |
| T1.5 RBAC enforcement layer (role + authority category-scope checks) | High | T1.3 | FR-3, BR-26/27 |
| T1.6 Admin: provision authority accounts + set category scope | Med | T1.5 | FR-2, `/users/authorities` |
| T1.7 2FA for authority/admin (optional per policy) | Med | T1.3 | FR-4 |
| T1.8 Login/OTP rate limiting + account lockout | Med | T1.2 | FR-4, NFR-13 |
| T1.9 Profile read/update, account deletion → **PII anonymization** | Med | T1.1 | P6, BR-33, C-14 |

**DoD (M1):** A citizen can register→verify→log in; an admin can provision a scope-limited authority; RBAC denies out-of-scope/role actions with `403`; sessions revoke immediately on logout/suspend; auth endpoints are rate-limited; auth flows covered by integration tests.

### P2 — Reporting & Media (M2)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T2.1 Report entity + validation (location required, media-or-description, in-city boundary) | Med | T1.5, T0.10 | FR-5/6, BR-2/3/35 |
| T2.2 `POST /reports` — synchronous fast write, returns `202 processing`, enqueue triage job | High | T2.1, T0.7 | FR-5, NFR-3, API §6.3 |
| T2.3 Idempotency-Key handling for submission | Med | T2.2 | BR-5, API §4.6 |
| T2.4 Media upload (`POST /media`), size/type limits, async processing | High | T0.2, T1.5 | FR-7, API §6.4 |
| T2.5 **EXIF/GPS stripping** + compression + thumbnail generation (worker) | Med | T2.4, T0.7 | P3 |
| T2.6 Attach media to report; media lifecycle states | Low | T2.4, T2.1 | Domain |
| T2.7 `GET /reports/{id}` + `GET /reports` (own-report tracking, spatial/status filters) | Med | T2.1 | FR-1, API §6.3 |
| T2.8 Pre-triage report edit / re-categorize; edit-lock after triage | Med | T2.1 | FR-11, BR (edit window) |
| T2.9 Submission rate limiting (anti-spam) | Med | T2.2 | FR-33, T3 |

**DoD (M2):** A report with a photo can be submitted and returns `202` immediately; the photo is stored with EXIF stripped and a thumbnail generated asynchronously; a citizen can retrieve and track their own reports; out-of-city and invalid submissions are rejected with correct codes; duplicate submits are idempotent.

> **Decision gate:** anonymous submission (PRD ❓Q4 / DM-Q2) must be resolved before T2.2 auth rules and T2.9 limits are finalized. Until then, default to authenticated-citizen submission and isolate the anonymous branch.

### P3 — Classification (M3)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T3.1 `ClassificationService` interface (category + severity signal + confidence + source) | Med | — | Arch §6 |
| T3.2 Hosted **LLM adapter** (provider-agnostic) with PII-minimized prompts | High | T3.1 | FR-9–13, A11/P7 |
| T3.3 Deterministic **keyword fallback** classifier (bilingual, admin-managed keywords) | Med | T3.1 | FR-13a |
| T3.4 LLM **cost/rate cap** + graceful degradation to fallback (never blocks intake) | High | T3.2, T3.3 | NFR-13, RISK-3 |
| T3.5 Classification worker job: consume submission, classify, persist result on Report | High | T3.2, T2.2 | Arch §4, FR-10 |
| T3.6 Timeout/retry/circuit-breaker on LLM calls | Med | T3.2 | NFR-4, Arch §12 |
| T3.7 Confidence handling + low-confidence flagging for later review | Med | T3.5 | FR-12, ❓Q10 |

**DoD (M3):** A submitted report is classified asynchronously with category + severity signal; when the LLM is unavailable or over budget, the keyword fallback produces a result and submission still succeeds; classification source is recorded; LLM failures degrade rather than crash the pipeline.

> **Decision gate:** LLM provider + no-training-data policy (PRD ❓Q9) and accuracy bar (❓Q10) inform T3.2/T3.7 but do not block the adapter abstraction.

### P4 — Clustering & Issues (M4)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T4.1 Issue entity + Report↔Issue relationship (severity/status/assignment on Issue only) | Med | T0.10 | Domain, BR |
| T4.2 Geospatial setup: `geography(Point,4326)`, GiST index, `ST_DWithin`/KNN queries | High | T0.2 | Arch §9, NFR-1 |
| T4.3 Clustering rules (per-category radius/time-window), admin-managed | Med | T4.1 | FR-18, ASSUMP-4 |
| T4.4 **Concurrency-safe find-or-create** clustering (spatial+category lock, geohash cell) | High | T4.2, T3.5 | Arch §4.3, race-safety |
| T4.5 Clustering worker step: run after classification (category required first) | High | T4.4, T3.5 | Arch §4.2 |
| T4.6 Issue severity = **max of member reports**; recompute on new member | Med | T4.5 | FR-14, BR-... |
| T4.7 Confirmation ("me-too"): one per citizen per issue; derived corroboration count | Med | T4.1, T1.5 | FR-16, BR-22/23, C-10 |
| T4.8 Proximity context (POIs near issue) — **display-only, never affects severity** | Med | T4.2 | FR-17, C-10 |

**DoD (M4):** Two nearby same-category reports cluster into one Issue without creating duplicates under concurrent submission; Issue severity reflects the max of its members; corroboration count is derived and read-only; proximity context is computed for display only; clustering runs strictly after classification.

> **Decision gate:** Critical severity band (PRD ❓Q2) affects T4.6 severity enum — isolate the enum so adding Critical is additive.

### P5 — Issue Triage Workflow (M5)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T5.1 Issue status state machine + transition validation | High | T4.1 | §6.3, BR-16, API §6.5 |
| T5.2 `PATCH /issues/{id}/status` (+ mandatory reason on reject/duplicate/etc.) | Med | T5.1 | FR-24, BR-19 |
| T5.3 Status Event emission (append-only history) | Med | T5.1 | FR-32, C-9 |
| T5.4 Assignment (`PATCH …/assignment`), scope-validated | Med | T5.1, T1.5 | FR-24, BR-26 |
| T5.5 **Severity override** (retains computed + override + actor + reason) | Med | T4.6 | FR-20, BR-20/21, C-8 |
| T5.6 Merge issues (+ reason) | High | T4.1 | FR-25 |
| T5.7 Split issues (each side keeps ≥1 report) | High | T4.1 | FR-25, BR-14/C-4 |
| T5.8 Internal vs public comments (RBAC-filtered) | Med | T1.5 | FR-24 |

**DoD (M5):** Authorities can move an Issue through its legal lifecycle (illegal transitions rejected with `409`), assign within scope, override severity with a mandatory reason (computed value preserved), and merge/split clusters; every transition writes an immutable status event; internal notes are hidden from citizens.

> **Decision gates:** definition of "Resolved" and reopen semantics (❓Q8 / DM-Q8) gate the resolve/reopen transitions in T5.1; merge/split re-attribution rules (DM-Q7) gate T5.6/T5.7 detail. Build the transitions; hold the specific gating rule for the answer.

### P6 — Notifications & Outbox (M6)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T6.1 **Transactional outbox** (event written in same tx as state change) | High | T5.3 | Arch §7, reliability |
| T6.2 Outbox dispatcher worker (at-least-once, idempotent consumers) | High | T6.1, T0.7 | Arch §7 |
| T6.3 Notification entity + generation on status change | Med | T6.1 | FR-27/29, BR-29 |
| T6.4 In-app delivery + `GET /notifications` + mark-read | Med | T6.3 | FR-27 |
| T6.5 Email channel adapter | Med | T6.3 | FR-29 |
| T6.6 SMS channel adapter — **gated to High severity server-side** | Med | T6.3 | BR-30, RISK-9 |
| T6.7 Notification preferences (opt-outs; SMS gate not bypassable) | Med | T6.4 | FR-28 |
| T6.8 SSE stream (`/notifications/stream`) for in-app real-time | Med | T6.4 | ASSUMP-3, API §6.11 |

**DoD (M6):** A status change reliably produces a notification even across a worker crash (outbox replays); citizens receive in-app notifications within SLA; email works; SMS fires only for High severity regardless of preference; preferences are honored; SSE pushes new notifications.

### P7 — Read Paths (M7)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T7.1 Authority **queue**: `GET /issues` sorted severity DESC then age, cursor pagination, scope filter | High | T5.1, T4.6 | FR-22, §5.1, API §4.4 |
| T7.2 Filtering/sorting/search allowlists on issues | Med | T7.1 | API §4.4 |
| T7.3 `GET /issues/{id}` detail + `GET /issues/{id}/reports` (paged members) | Med | T4.1 | API §6.5 |
| T7.4 Map endpoint `GET /map/issues` as GeoJSON + server-side aggregation at low zoom | High | T4.2 | FR-23, API §6.9 |
| T7.5 Analytics `GET /analytics/summary` (counts, time-to-resolution, scope-limited) | Med | T5.3 | FR-26 |

**DoD (M7):** The queue returns correctly ranked, scope-filtered, paginated Issues; the map returns valid GeoJSON that aggregates at low zoom to bound payload; analytics reflect real status-event data; citizens see only the public subset (pending ❓Q7).

> **Decision gate:** public visibility granularity (PRD ❓Q7 / DM-Q3) gates the public subset in T7.1/T7.3/T7.4.

### P8 — Moderation, Audit & Reference Data (M8)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T8.1 **Append-only audit log** for privileged actions + `GET /audit-events` | High | T5.x | FR-32, NFR-10, C-9 |
| T8.2 Moderation actions on report/issue/media/comment (hide/remove + reason → `410`) | Med | T5.8, T2.4 | FR-31 |
| T8.3 Reference data admin CRUD: categories, POIs, severity keywords, clustering rules | Med | T1.6 | FR-30, NFR-11 |
| T8.4 City boundary management endpoint | Low | T4.2 | BR-35 |
| T8.5 `GET /meta/enums` (severities/statuses/categories/types) for client sync | Low | — | API §6.16 |

**DoD (M8):** Every privileged action is audited immutably and queryable (admin: all; authority: own); moderated content returns `410`; admins manage all reference data; keyword/clustering-rule changes flow into P3/P4 behavior for future processing.

### P9 — Export (M9)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T9.1 Async export jobs (`POST /exports`, `GET /exports/{id}`), CSV/GeoJSON | Med | T7.5, T0.7 | NFR-12, API §6.15 |
| T9.2 Short-lived signed download URLs (scope-limited data) | Med | T9.1 | API §9 security |

**DoD (M9):** Authorities/admins can request scope-limited exports that generate asynchronously and download via an expiring signed link.

### P10 — Hardening, Security, Performance & Deployment (M10)
| Task | Cx | Dep | Traces |
|------|----|-----|--------|
| T10.1 Global rate limiting + LLM cost-cap verification under load | High | all | NFR-13 |
| T10.2 Security review: authZ coverage, IDOR, enumeration, input validation, TLS/HSTS | High | all | API §8, NFR-5 |
| T10.3 Privacy review: EXIF, PII minimization to LLM, anonymization, PII in responses | Med | all | P1–P7 |
| T10.4 Performance/load test: submission throughput, queue/map query latency, geospatial indexes | High | P7 | NFR-1/2/3 |
| T10.5 Failure-mode drills: LLM down, worker crash, DB failover, outbox replay | Med | P6 | Arch §12 |
| T10.6 Observability: metrics, alerts on SLA/cost/queue-depth | Med | all | NFR-9 |
| T10.7 Deployment automation + rollback + migration-on-deploy strategy | High | T0.5 | NFR-9 |
| T10.8 Backups/restore drill for DB + object store | Med | T0.2 | NFR-10 |

**DoD (M10):** All checklists in §9 pass; system meets NFR latency/throughput targets under load; security and privacy reviews are clean; deploy + rollback + restore are rehearsed.

---

## 6. Consolidated Dependency Map & Critical Path

**Critical path (longest dependency chain):**
```
P0 foundation → P1 auth/RBAC → P2 report submission → P3 classification
→ P4 clustering → P5 triage workflow → P6 outbox+notifications → P7 read paths
→ P10 hardening/deploy
```

Key hard dependencies to respect (rework risks if violated):
- **Classification (P3) before Clustering (P4)** — clustering keys on category (Arch §4.2).
- **RBAC (P1) before any authority/admin feature** (P5–P9).
- **Issues (P4) before queue/map/analytics (P7)** — nothing to display otherwise.
- **Outbox (P6.1) before Notifications (P6.3+)** — reliability primitive first.
- **Status events (P5.3) before Analytics (P7.5)** — time-to-resolution derives from them.
- **Geospatial setup (P4.2) before clustering, map, and spatial filters.**

Parallelizable with 2 engineers:
- During P0/P1: one engineer on platform/CI (P0), the other on auth/data modeling.
- After P4: read paths (P7) and notifications (P6) can proceed in parallel once status events exist.
- Reference-data admin CRUD (P8.3) can begin early where its consumers (keywords P3.3, clustering rules P4.3) need it — build the config surfaces alongside the consumers.

---

## 7. Risks & Mitigation Strategies

| ID | Risk | Impact | Mitigation | Phase |
|----|------|--------|-----------|-------|
| R-1 | LLM cost/latency spikes or outage (RISK-3) | Triage stalls / budget blown | Cost/rate caps + keyword fallback + circuit breaker; submission never blocks | P3 |
| R-2 | Duplicate Issues under concurrent submission | Data integrity, bad queue | Spatial+category lock find-or-create; test under concurrency early | P4 |
| R-3 | Notification loss on crash (SLA breach) | Citizen trust, FR-29 miss | Transactional outbox + at-least-once idempotent dispatch | P6 |
| R-4 | Geospatial performance at scale | Slow queue/map | PostGIS GiST indexes, `ST_DWithin`, server-side map aggregation, load test | P4/P7 |
| R-5 | Unresolved open questions block phases | Rework / stalls | Decision gates in §10 with owners; isolate affected branches behind abstractions | All |
| R-6 | RBAC/authZ gaps (privilege escalation, IDOR) | Security breach | AuthZ built per-slice + dedicated security review; opaque IDs; `404`-hiding | P1/P10 |
| R-7 | Privacy leakage (EXIF, PII to LLM, PII in responses) | Legal/ethical, P1–P7 | EXIF strip by default, prompt minimization, response field discipline, privacy review | P2/P3/P10 |
| R-8 | SMS abuse/cost | Budget | Server-side High-severity gate, not preference-bypassable | P6 |
| R-9 | Scope creep beyond approved features | Timeline, grading | This plan schedules only traced work; changes require doc updates first | All |
| R-10 | 2-person bandwidth / capstone timeline | Missed milestones | MVP-first ordering; P8/P9 are trimmable; buffer in P10 | All |
| R-11 | Bangla/Banglish handling weakness | Misclassification | UTF-8 end-to-end, bilingual keyword fallback, test corpus in both | P3 |

---

## 8. Testing & Documentation Checkpoints

### 8.1 Testing checkpoints (per phase)
| Phase | Testing focus |
|-------|---------------|
| P0 | Migration up/down, CI gate, health check, config loading |
| P1 | Auth flows, session revocation, RBAC allow/deny matrix, rate-limit/lockout |
| P2 | Submission validation, `202` async contract, idempotency, EXIF-strip verification, upload limits |
| P3 | Classification result shape, **fallback triggers when LLM down/over-budget**, cost-cap behavior, bilingual inputs |
| P4 | **Concurrency test** for find-or-create (no duplicate Issues), severity=max, corroboration count derivation, spatial query correctness |
| P5 | State-machine legal/illegal transitions, mandatory-reason enforcement, override retains computed value, merge/split invariants |
| P6 | Outbox replay after simulated crash, at-least-once idempotency, SMS High-severity gate, preference honoring, SSE delivery |
| P7 | Queue ordering (severity→age), cursor pagination stability under mutation, GeoJSON/map aggregation, analytics accuracy |
| P8 | Audit immutability & completeness, moderation `410`, reference-data propagation |
| P9 | Export correctness, signed-URL expiry, scope limiting |
| P10 | Load/perf vs NFR targets, security & privacy review, failure-mode drills, backup restore |

**Standing test requirements (all phases):** unit + integration tests in CI; contract tests against the API spec (§04) so responses match documented schemas/status codes; regression suite grows each phase.

### 8.2 Documentation checkpoints
| Checkpoint | When | Content |
|-----------|------|---------|
| DC-1 | End P0 | Environment/setup + runbook skeleton; migration guide |
| DC-2 | End P1 | Auth & RBAC docs; role/scope matrix confirmed against PRD §4.2 |
| DC-3 | Per feature phase | Update API reference to match implementation; note any deviation back to the spec (spec is source of truth — if code must differ, the spec is amended first) |
| DC-4 | End P3/P4 | Triage pipeline operational doc (classification, fallback, clustering behavior, tuning knobs) |
| DC-5 | End P6 | Notification/outbox operations + SLA monitoring |
| DC-6 | End P10 | Deployment runbook, rollback, restore, on-call/observability guide |
| DC-7 | Continuous | Open-question resolution log (§10) — record each decision and the doc it updated |

---

## 9. Deployment Readiness Checklist

- [ ] All migrations apply cleanly from zero and are reversible; migration-on-deploy strategy defined.
- [ ] Environment configs/secrets managed per environment; no secrets in code.
- [ ] TLS/HSTS enforced; secure session cookie flags verified in production config.
- [ ] Rate limiting active on auth, submission, and LLM-triggering paths; limits documented.
- [ ] LLM cost caps + fallback verified in a staging failure drill.
- [ ] Transactional outbox dispatcher verified for at-least-once delivery after crash.
- [ ] Geospatial indexes present and query plans validated under representative data.
- [ ] Backups scheduled (DB + object store); restore rehearsed.
- [ ] Observability: metrics, dashboards, and alerts for latency SLA, queue depth, LLM cost, error rate.
- [ ] Health/readiness endpoint reflects dependency degradation.
- [ ] Deployment automation with tested rollback.
- [ ] Security review sign-off (authZ, IDOR, enumeration, input validation).
- [ ] Privacy review sign-off (EXIF strip, PII minimization to LLM, anonymization, response PII discipline).
- [ ] Load test meets NFR-1/2/3 targets.

---

## 10. Open-Question Decision Gates (Blocking Dependencies)

These must be resolved by the noted phase; each is isolated behind an abstraction so a late answer is additive, not a rewrite.

| Open item | Blocks | Needed by | Owner (proposed) |
|-----------|--------|-----------|------------------|
| PRD ❓Q4 / DM-Q2 — anonymous reporting | Submission auth rules, rate limits | Start of P2 | Team + supervisor |
| PRD ❓Q9 — LLM provider + no-training-data policy | LLM adapter config | Mid P3 | Team |
| PRD ❓Q10 — accuracy bar | Confidence thresholds, low-confidence flagging | End P3 | Team + supervisor |
| PRD ❓Q2 — Critical severity band | Severity enum, queue ordering | Before P4.6 | Team |
| PRD ❓Q8 / DM-Q8 — "Resolved" definition & reopen semantics | Resolve/reopen transitions | Before P5.1 finalization | Team + supervisor |
| DM-Q7 — merge/split re-attribution | Merge/split detail | Before P5.6/5.7 | Team |
| DM-Q5 — confirmation revocability | Confirmation delete endpoint, count monotonicity | Before P4.7 finalization | Team |
| PRD ❓Q7 / DM-Q3 — public visibility granularity | Public read subset, map/queue exposure | Before P7 public reads | Team + supervisor |

---

## 11. Final Project Completion Checklist

**Functional completeness (traced to approved docs):**
- [ ] All MUST-have FRs implemented and tested; SHOULD/COULD items either done or explicitly deferred with rationale.
- [ ] Report→Classify→Cluster→Triage→Notify pipeline works end-to-end.
- [ ] Report/Issue separation preserved (severity/status/assignment only on Issue).
- [ ] Severity-only model (no numeric scoring); corroboration/proximity display-only.
- [ ] RBAC + authority category-scoping enforced everywhere; audit log complete.

**Quality & non-functional:**
- [ ] NFR latency/throughput/geospatial targets met under load.
- [ ] Reliability primitives (outbox, fallback, retries) verified via failure drills.
- [ ] Security and privacy reviews signed off.
- [ ] Test coverage: unit + integration + API contract + concurrency + regression, all green in CI.

**Delivery & docs:**
- [ ] API implementation matches `04-api-specification.md`; deviations reconciled in the spec.
- [ ] Deployment, runbook, and observability docs complete (DC-6).
- [ ] Open-question resolution log complete (DC-7); no unresolved gate remains open.
- [ ] Demo scenario rehearsed for capstone defense.
- [ ] Handover package: source, migrations, env setup, planning docs 01–05.

---

## 12. Self-Review (Principal Engineer / Tech Lead)

*(Verifying order correctness, respected dependencies, no implementation gaps, and realism/maintainability — as required.)*

### Are tasks in the correct order?
- ✅ Foundations (P0) and auth/RBAC (P1) precede all features. Reliability primitives (outbox P6.1) precede notification features. Read paths (P7) follow the write path (P4/P5). No feature depends on something scheduled later.
- ✅ The **classification-before-clustering** invariant (Arch §4.2) is encoded as P3→P4 and as an explicit critical-path note.

### Are dependencies respected?
- ✅ §6 maps hard dependencies; each task lists `Dep`. Geospatial setup (P4.2) precedes every spatial consumer. Status events (P5.3) precede analytics (P7.5). RBAC precedes all privileged features.
- ⚠️ **Reference data timing:** severity keywords (P3.3) and clustering rules (P4.3) need admin CRUD (P8.3). Resolved by building the *config surfaces* alongside their consumers in P3/P4 and completing the *admin management UX/endpoints* in P8 — flagged in §6 so it isn't missed.

### Any implementation gaps?
- ✅ Every approved API resource and FR maps to a task (traceability columns throughout). Cross-cutting concerns (validation, rate limiting, audit, privacy) are embedded per phase, not deferred.
- ✅ Deliberate non-tasks are explicit: **no `POST /issues`** (Issues arise from clustering), no scoring engine, no external webhooks — consistent with the approved docs, so their absence is intentional, not a gap.
- ⚠️ **Bulk status update** (noted in API §9 self-review as a future additive endpoint) is intentionally **not** scheduled to avoid introducing scope; recorded here so it is a conscious deferral, not an oversight.

### Is the plan realistic and maintainable?
- ✅ Estimates are relative and sized for a 2-person team; P8/P9 are identified as trimmable if the timeline compresses, without touching the core pipeline (MVP protection).
- ✅ Open questions are handled as **decision gates behind abstractions** (§10), so late answers are additive — directly serving the "minimize rework" objective.
- ⚠️ **Biggest realism risk** remains bandwidth (R-10): the critical path P0→P10 is long for two people. Mitigation: strict MVP ordering, parallelizable P6/P7, and deferrable P8/P9. Recommend the team confirm which SHOULD/COULD FRs are in-scope for the defense before P5, so late phases can be cut cleanly rather than rushed.

### Improvements applied in this revision
- Added explicit **decision gates with owners** (§10) tied to the phases they block.
- Embedded **contract testing against the API spec** as a standing requirement so implementation cannot silently drift from doc 04.
- Made **reference-data sequencing** explicit to avoid a hidden P3/P4 ↔ P8 dependency.

---

*End of `docs/05-project-plan.md` (v1.0). This roadmap schedules only work traceable to the approved PRD, Architecture, Domain Model, and API Specification; it introduces no new features or requirements. Unresolved open questions are tracked as decision gates (§10) to be closed before their dependent phases. This completes the 5-document planning set (01–05).*
