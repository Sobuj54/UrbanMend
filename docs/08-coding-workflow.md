# Coding Workflow

> Applies after the design phase is complete.
> Stack is committed in [07-adr-001-app-framework.md](07-adr-001-app-framework.md): Python + Django + DRF.

## Steps

### 1. Scaffold Project
- Create the Django project (`urbenmend`) and one app per module — see [02-architecture.md](02-architecture.md) §2.4
- Settings split (`base`/`dev`/`prod`) loaded via `django-environ` (T0.3)
- Pin every line of `requirements.txt`
- Configure `.env.local` (git-ignored) and commit `.env.example` with placeholders
- Configure lint: `ruff check` + `ruff format --check`, and `mypy` (T0.5)
- Docker Compose is the mandated local environment — GeoDjango needs GDAL/GEOS/PROJ

### 2. Setup Database
- PostgreSQL + PostGIS; use the `postgis/postgis` image, not plain `postgres`
- First migration enables the PostGIS extension (T0.4)
- ⚠️ Declare the **custom user model before the first migration** — irreversible afterwards (T0.10)
- Define models, generate migrations with `makemigrations`, apply with `migrate`
- Add seed/reference data as data migrations or fixtures (taxonomy, POIs, keywords are config — NFR-11)

### 3. Core Boilerplate
- ASGI entry point (`urbenmend.asgi:application`) served by uvicorn
- Django middleware; DRF configured with `SessionAuthentication`
- Auth: **server-validated sessions** on the `cached_db` backend + CSRF on unsafe methods
  — **not JWT**; [02-architecture.md](02-architecture.md) §8 requires immediate revocation
- Standardised error envelope and cursor pagination class matching
  [04-api-specification.md](04-api-specification.md) §1.3/§4.1 (T0.6)
- Celery app + beat (`celery -A urbenmend worker -B`)

### 4. Build Feature-by-Feature (Vertical Slice)
For each feature, in order:
```
model → migration → selectors.py / services.py → serializer → view + url → contract test
```
Business rules, RBAC checks, and transactions live in `services.py` (writes) and
`selectors.py` (reads). DRF views stay thin; DRF permission classes are defence-in-depth,
**not** the enforcement point (FR-3).

### 5. Tests Per Feature
- Unit tests — `pytest`, fast and in-process, no external deps
- Integration tests — `pytest` (`pytest-django` + `factory_boy`) against real PostGIS/Redis/storage
- Contract tests — responses must match [04-api-specification.md](04-api-specification.md) schemas and status codes

### 6. Git Discipline
- One feature or fix per branch (`feature/*`)
- Small, focused commits
- PR required; no direct push to `main`

### 7. CI Pipeline
- Auto-run lint, type-check, and tests on every push
- Include `makemigrations --check --dry-run` (model drift) and `check --deploy`
- Block merge on failure

### 8. API Documentation
- Document each endpoint as it is built, against the existing spec
- Keep README updated

### 9. Design Drift Check
- Periodically compare implementation against the planning docs
- The spec is authoritative over the implementation — if code must differ, amend the doc first

---

## Rule

**Start with Auth/User** — it is foundational. Build all dependent features after.
