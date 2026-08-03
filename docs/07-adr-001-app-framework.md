# UrbanMend — ADR-001: Application Framework (Django + DRF)

> Records the resolution of ASSUMP-1 (`docs/02-architecture.md` §2.3) and supersedes its
> previous "Python/FastAPI *(primary rec)* or TypeScript/NestJS *(viable alt)*" wording.

| | |
|---|---|
| **Document** | `docs/07-adr-001-app-framework.md` |
| **Status** | **Accepted** |
| **Date** | 2026-08-03 |
| **Author role** | Principal Backend Architect (on behalf of the team) |
| **Supersedes** | ASSUMP-1 in `02-architecture.md` (as worded in v1.1) |
| **Source of truth** | `01-prd.md` §16.5 · `02-architecture.md` §2.3/§3.1/§8/§9 |
| **Consumed by** | `02-architecture.md` (v1.2) · `05-project-plan.md` (v1.2) · `06-devops-guide.md` (v1.1) · provenance notes in `01-prd.md` (v1.3), `03-data-model.md` (v1.2), `04-api-specification.md` (v1.2) |
| **Scope** | Application framework for the API process + Worker process. Client/UI is out of scope. |

---

## 1. Context

PRD §16.5 delegated the *language/framework* choice to the architecture document, which in
turn left it as **ASSUMP-1** — a recommendation to confirm. That assumption is now closed:
the team has committed to **Python + Django + Django REST Framework**.

The committed stack is:

- **Web framework** — Django + Django REST Framework (DRF)
- **Geospatial** — GeoDjango (`django.contrib.gis`) + `djangorestframework-gis`
- **Async worker / queue** — Celery (Redis broker) + Celery beat
- **Object storage** — `django-storages` (S3 / MinIO)
- **Media processing** — Pillow (orient → strip EXIF → compress → thumbnail)
- **Auth extras** — `django-otp` (2FA, FR-4), Argon2 password hashing (NFR-5)
- **Configuration / secrets** — `django-environ` (environment-driven, NFR-5)
- **Observability** — `structlog` (structured JSON logs + `traceId`), `django-prometheus`
  (`/metrics`), OpenTelemetry instrumentation (NFR-9)
- **App server** — ASGI (uvicorn) so the SSE notification stream (ASSUMP-3) does not pin
  sync worker threads
- **Testing** — `pytest-django` + `factory_boy` (unit/integration), contract tests against
  `04-api-specification.md` (NFR-9)
- **Migrations** — Django migrations (T0.4); the first migration enables the PostGIS extension

---

## 2. Decision Drivers

| Driver | Source | How Django satisfies it |
|--------|--------|-------------------------|
| Server-validated sessions with **immediate revocation** | Architecture §8, BR-25/BR-33 | Django's session framework (`cached_db` backend) *is* this design — opaque token in a `Secure`/`HttpOnly`/`SameSite` cookie, revocable by deleting session rows. |
| First-class geospatial (NFR-1) | NFR-1, FR-16/17/18/23 | GeoDjango is the most mature spatial ORM available; `geography`-typed columns, GiST indexes, `ST_DWithin`/KNN/grid aggregation without hand-written SQL. |
| RBAC on every server action (FR-3) | FR-3, BR-26 | Explicit `role` field + authority↔category scope checked in the service layer (see §4), rather than `django.contrib.auth` Groups/Permissions, which cannot express category scoping. |
| Reference-data management (FR-30/31) | FR-30, FR-31 | Django admin provides the CRUD surface largely out of the box, directly relieving the 2-person bandwidth risk (R-10). |
| Registration, verification, password reset, hashing (FR-1) | FR-1, NFR-5 | Built in (custom user model + Argon2). |
| Migrations (T0.4) | NFR-9 | Built in and versioned. |
| CSRF for cookie-based auth (T1.4) | API §2 | DRF `SessionAuthentication` enforces CSRF on state-changing requests. |
| 2FA (FR-4, optional) | FR-4 | `django-otp`. |

---

## 3. Alternatives Considered

### 3.1 Python + FastAPI — rejected as primary

| Strength | Why it lost to Django |
|----------|----------------------|
| Pydantic + auto-OpenAPI would map cleanly onto the precise contracts in `04-api-specification.md`; contract tests would be simpler. | Real, but it buys an API contract at the cost of re-building everything Django gives free (sessions, admin, RBAC scaffolding, migrations) — and the contract work is bounded, whereas the sessions/admin/bandwidth wins compound across all ten phases. |
| Native async suits long LLM calls. | The LLM work runs in the **worker**, where blocking is the norm; the API write path is intentionally synchronous and cheap (NFR-3). |

FastAPI remains a defensible choice for a team that knows it well; the deciding factors here
were the session model, the admin-derived reference-data surface, and bandwidth (R-10).

### 3.2 TypeScript + NestJS — rejected

| Strength | Why it lost |
|----------|-------------|
| Strong typing and structure; good if the team preferred TS. | **Prisma has no first-class `geography`/geometry type.** Spatial columns must be declared `Unsupported(...)` and excluded from the generated client, so every geospatial query in Architecture §9 (`ST_DWithin`, KNN `<->`, server-side grid aggregation, point-in-polygon) becomes raw SQL — forfeiting type safety exactly where the hardest requirements live. TypeORM/Drizzle are better but still thin on PostGIS. |
| NestJS dependency injection scales well to modular monoliths. | True, but the module boundaries are already enforced by the codebase layout; a DI container is not what protects them. |

> ⚠️ **Verification note:** the Prisma/PostGIS status above was assessed from knowledge as of
> mid-2026 and was **not re-verified live** (web access was unavailable at decision time).
> The claim should be re-checked before this ADR is considered fully signed off; the decision
> is not expected to change, but the record should be accurate.

### 3.3 Other stacks

Go (stdlib + PostGIS via `golang-migrate`) and Java/Spring were noted and set aside: they are
viable but have weaker LLM-ecosystem ergonomics and a steeper start for this team, and they
offer no advantage on the geospatial requirement over GeoDjango.

---

## 4. Consequences / Accepted Costs

| Cost | Mitigation |
|------|------------|
| DRF serializers are more verbose than Pydantic for the precise envelope in `04-api-specification.md` (§4.1 error model, cursor pagination). | A custom pagination class + custom exception handler are specified in the plan (T0.6). |
| Django's idiom (fat models, logic in views/serializers) pushes against the API → service → data-access layering mandated in Architecture §3.1. | Convention from day one: thin DRF views, `services.py` (writes + authorization) and `selectors.py` (reads) per app; DRF permission classes are defence-in-depth, **not** the enforcement point (FR-3). |
| Sync-first framework; a long-lived SSE stream under WSGI would pin a worker thread. | Run ASGI (uvicorn); make only the stream endpoint async. Polling remains the ASSUMP-3-sanctioned fallback. |
| GeoDjango needs GDAL/GEOS system libraries. | Develop and deploy in Docker (`postgis/postgis` image, `python:3.x-slim` runtime with system GDAL/GEOS). Notoriously awkward natively on Windows — this team's platform — so containers are the development environment of record. |
| `RunPython` data migrations are not auto-reversible. | The DevOps guide's "keep a `migrate down` for every migration" rule is amended to require explicit reverse functions (see `06-devops-guide.md` §7). |

**Outcome of the decision:**

- ASSUMP-1 is **RESOLVED**. The architecture's statement that it "does not depend on the
  framework choice" (§2.3) is retained but reframed: the module boundaries remain
  framework-agnostic, while the framework itself is no longer open.
- No `FR-x` / `NFR-x` / `BR-x` changes, no new endpoints or entities. This ADR records a
  technology decision only.
- Framework-specific detail is confined to the API and persistence layers, as §2.3 always
  required; the module decomposition in Architecture §3 and the API contract in
  `04-api-specification.md` are unaffected.

---

## 5. References

- `docs/01-prd.md` §16.5 (delegation of stack choice)
- `docs/02-architecture.md` §2.3 (ASSUMP-1 — superseded), §3.1 (layering), §8 (sessions),
  §9 (geospatial), §14 (self-review)
- `docs/05-project-plan.md` (task notes updated for Django), `docs/06-devops-guide.md`
  (container/CI/migration patterns retargeted)

*End of `docs/07-adr-001-app-framework.md` (v1.0, Accepted).*
