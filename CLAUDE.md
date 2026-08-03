# UrbanMend — Claude Code Project Memory

Civic issue-reporting platform for a single city (initial deployment: Bangladesh). Citizens submit
geolocated photo reports of infrastructure problems; a hosted-LLM triage layer assigns category and
severity (Critical/High/Medium/Low), and nearby reports cluster into Issues that Authorities act on.
Three actors: **Citizen**, **Authority**, **Admin**. This repo is **backend only** (API + Worker).

**Two domain terms that are not synonyms** (PRD §6.1): a **Report** is one citizen submission;
an **Issue** is a cluster of one or more Reports = one real-world problem. Severity, status, and
assignment live on the **Issue**, never on the Report.

**Naming:** the product is "UrbanMend"; the code/package/image identifier is `urbenmend`
(`urbenmend.asgi:application`, `celery -A urbenmend`, `urbenmend/api`).

## Stack (committed — 02-architecture.md §2.3, ADR-001 Accepted)

| Layer | Choice |
|---|---|
| Language / framework | Python + Django + Django REST Framework |
| Datastore | PostgreSQL + PostGIS |
| Geospatial | GeoDjango (`django.contrib.gis`) + `djangorestframework-gis` |
| Migrations | Django migrations |
| App server | ASGI (uvicorn) |
| Worker / queue | Celery (Redis broker) + Celery beat |
| Cache / rate-limit store | Redis |
| Object storage | S3-compatible (S3 or MinIO) via `django-storages` |
| Lint / types | `ruff`, `mypy` |
| Tests | `pytest-django` + `factory_boy` |

Not pinned anywhere in the docs: Python version (`3.12` is labelled an example; real pin is T0.1),
Django/DRF versions, package manager (`pip` + `requirements.txt` appear only in a Dockerfile
example), CI vendor, cloud host, LLM provider. **Do not invent these — raise them instead.**

## Context: planning docs

`@docs/08-coding-workflow.md`

The rest are large; **read on demand** rather than loading every session:

| Path | Read when |
|---|---|
| `docs/01-prd.md` | Requirements, FR-/NFR-/BR- IDs, non-goals, edge cases |
| `docs/02-architecture.md` | Module boundaries, layering, module→Django-app map, sessions, outbox |
| `docs/03-data-model.md` | Domain entities and relationships (**domain only — no schema/SQL**) |
| `docs/04-api-specification.md` | Endpoint contracts — **authoritative over the implementation** |
| `docs/05-project-plan.md` | Phases and task IDs (T0.1, T1.5, …) |
| `docs/06-devops-guide.md` | Containers, CI stages, migration policy, K8s, observability |
| `docs/07-adr-001-app-framework.md` | Why Django/DRF; why FastAPI and NestJS were rejected |

## Repo structure

**Today the repo contains only `docs/` and `.git`. No source code, no dependency manifest.**

Planned layout (from `02-architecture.md` §2.4 — not yet built):

```
manage.py
requirements.txt
docker-compose.yml            # mandated local env (GDAL/GEOS/PROJ on Windows)
urbenmend/                    # Django project: asgi.py, celery.py, settings split
identity/  reporting/  media/  classification/  issues/  geo/
notifications/  moderation/  audit/  export/  platform/
```

One Django app per architecture module. Each app carries **`services.py` (writes + authorization)**
and **`selectors.py` (reads)** from day one; DRF views stay thin.

✅ Resolved (A1): the settings split is **`base`/`dev`/`prod`** (plan T0.3 naming). DevOps §3.2's
`urbenmend.settings.local` was amended to `urbenmend.settings.dev`. Do not reintroduce `settings.local`.

## Commands

Sourced from `docs/06-devops-guide.md` §4.1 and its Dockerfile example. **No manifest or task
runner exists yet, so none of these are machine-verified.**

```bash
ruff check && ruff format --check          # lint
mypy                                       # type-check
pytest                                     # unit + integration
python manage.py makemigrations --check --dry-run   # model drift
python manage.py check --deploy            # security config
python manage.py migrate                   # apply migrations
python manage.py collectstatic --noinput   # build-time only
uvicorn urbenmend.asgi:application --host 0.0.0.0 --port 8080   # api
celery -A urbenmend worker -B --loglevel=info                   # worker + beat
docker build .                             # image
```

## Conventions (detail in `.claude/rules/`)

- URI versioning `/api/v1`; plural lowercase resource nouns; `camelCase` JSON bodies; ISO-8601 UTC
- Collections return `{ data, page, meta }` with **cursor** pagination (mandatory, limit 20/max 100);
  single resources return the bare object
- Errors return `{ error: { code, message, details, traceId } }`
- Auth is **server-validated sessions** in a `Secure`/`HttpOnly`/`SameSite` cookie + CSRF — not JWT
- Authorization is enforced in the **service layer**, on every mutating and sensitive-read action
- Opaque server-generated IDs in URLs; never sequential or guessable

## Do not

- **Do not use JWT.** Sessions are required for immediate revocation (Arch §8).
- **Do not add `POST /issues`.** Issues form only via async clustering.
- **Do not add write endpoints for status-events or audit-events** — append-only (C-9, BR-31).
- **Do not hard-delete** users, categories, POIs, or Issues. Retire; user deletion anonymizes (C-14).
- **Do not let POI/proximity data affect severity or ordering** — display-only (C-10).
- **Do not add outbound webhooks or government-system integration** — PRD §2.2 non-goal.
- **Do not add a numeric priority score or tunable weights** — explicitly removed (FR-21).
- **Do not use `django.contrib.auth` Groups/Permissions for RBAC** — cannot express BR-26 scoping.
- **Do not run `migrate`** in the Dockerfile or container entrypoint.
- **Do not deploy `latest`** — always a SHA-tagged image.
- **Do not expose `/metrics`** publicly or on the Ingress.
- **Do not set `readOnlyRootFilesystem: true`** without an `emptyDir` at `/tmp` — breaks uploads.
- **Do not leave `DEBUG` enabled** in any deployed environment.
- **Do not commit secrets.** `.env.local` is ignored; `.env.example` holds placeholders only.
- **Do not add frontend code.** Plan and DevOps guide both scope this repo to backend only.
- **Do not let the code diverge from `docs/04-api-specification.md`** — amend the spec first.
- **Do not invent answers to open questions** Q1 (taxonomy), Q3 (POI source), Q5 (notification
  channels), Q6 (EXIF default), Q10 (accuracy bar). Flag them.

## Path-scoped rules

`.claude/rules/` loads automatically by file pattern: `api-conventions.md`, `auth.md`,
`database.md`, `async-worker.md`, `testing.md`.
