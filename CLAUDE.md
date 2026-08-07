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

✅ Pinned in T0.1 (2026-08-03): **Python 3.13**, **Django 5.2.16 LTS**, **DRF 3.17.1**, deps via
**pip-compile** (`requirements/{base,dev}.in` → `.txt`, `--generate-hashes`; `dev.txt` also needs
`--allow-unsafe`). Base image `python:3.13-slim` (verified: GEOS 3.13.1, GDAL 3.10.3). Python 3.13
is a ceiling, not a preference — `djangorestframework-gis` 1.2.1 caps at 3.13.

✅ Pinned in A9/T0.5 (2026-08-05): **CI vendor is GitHub Actions** — chosen by the user, recorded
here because no planning doc named one. The seven-stage order is doc-mandated (DevOps §4.1) and
vendor-independent; only the runner syntax is Actions-specific.

Still not pinned anywhere: cloud host, LLM provider (Q9).
**Do not invent these — raise them instead.**

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
| `docs/09-operations.md` | **DC-1** — local setup, env vars, runbook skeleton, migration guide |

## Repo structure

✅ Built in A4–A5 (2026-08-03). Actual layout:

```
manage.py  pyproject.toml  Dockerfile  docker-compose.yml  requirements/
urbenmend/
  settings/{base,dev,prod,build}.py  urls.py  asgi.py  wsgi.py
  identity/  reporting/  media/  classification/  issues/  geo/
  notifications/  moderation/  audit/  export/  platform/
```

⚠️ Apps are **nested under `urbenmend/`** and import as `urbenmend.<label>` — a root-level
`platform` package shadows the stdlib module Django imports. App labels are unchanged.

Each app carries `apps.py`, `models.py`, **`services.py` (writes + authorization)**,
**`selectors.py` (reads)**, `admin.py`, `migrations/` and `tests/` from day one. DRF views stay thin.
`urbenmend/platform/tests/test_app_skeleton.py` enforces this structurally — add the same file set
when adding an app, or it fails.

✅ Resolved (A1): the settings split is **`base`/`dev`/`prod`** (plan T0.3 naming). DevOps §3.2's
`urbenmend.settings.local` was amended to `urbenmend.settings.dev`. Do not reintroduce `settings.local`.

✅ Built in A4 (2026-08-03): `urbenmend/settings/{base,dev,prod,build}.py`, `urbenmend/{urls,asgi,wsgi}.py`,
`pyproject.toml` (ruff/mypy/pytest config). Verified: `check --deploy` clean on prod, `mypy --strict` clean,
uvicorn boots. Three A4 decisions that bind later work:

- **Apps nest under `urbenmend.`** (`urbenmend/platform/`, not `platform/`) — a root-level `platform`
  package shadows the stdlib module Django imports. App *names* are unchanged.
- **`DJANGO_SECRET_KEY` and `DATABASE_URL` are required with no fallback** in `base`/`prod`. `build.py`
  injects throwaway values before importing base so build-time `collectstatic` needs no secrets;
  `dev.py` has local-only fallbacks so a fresh clone can lint and test. Never add one to `prod.py`.
- **`DEFAULT_PAGINATION_CLASS` is unset**, `rest_framework.W001` silenced. No DRF built-in emits the
  `{data, page, meta}` envelope; T0.6 adds the custom class and removes the silencer.

✅ Built in A6 (2026-08-04): `AUTH_USER_MODEL = "identity.User"` — **the irreversible step is done.**
`urbenmend/identity/models.py` holds `User` (UUID PK), `UserManager`, and the `Role` / `UserStatus` /
`Language` enums; `admin.py` and 28 tests accompany it. Decisions that bind later work:

- **`email` and `phone` are both nullable + UNIQUE**, at-least-one enforced by the
  `identity_user_has_contact_or_anonymized` CheckConstraint. Absence is **`NULL`, never `""`** —
  Postgres allows many NULLs under UNIQUE but only one `""`. The constraint has a `status=deleted`
  escape hatch; without it the C-14/BR-33 anonymization would be impossible. Don't tighten it.
- **`is_active` is a derived read-only property**, not a column — assignment raises `AttributeError`
  by design; move `status` instead. `registered`/`verified`/`active` are the authenticating states.
- **Verification is timestamps** (`email_verified_at`, `phone_verified_at`), not booleans. The API's
  `verified: {email, phone}` is derived from them.
- **Contact normalization lives in `save()`**, not just `clean()` — DRF never calls `full_clean()`.
- ⚠️ **`PermissionsMixin` is inherited for Django-admin plumbing only.** Domain RBAC is `role` +
  category scope in `services.py`. Never put it in `groups`/`user_permissions`.
- **Still deliberately absent:** the Authority↔Category M2M (waits for Category, T0.10 — an ordinary
  additive migration later) and the T1/T2 trust signal (derivable; a stored score would invent a
  weighting, and FR-21 removed the one tunable score).
- Tooling: `django_otp.*` is `ignore_missing_imports` in mypy (no `py.typed`; pulled in by the
  django-stubs plugin, not by our imports); `**/tests/*.py` is exempt from ruff `S105`/`S106`.

✅ Built in A7 (2026-08-05): `urbenmend/identity/migrations/0001_initial.py` — **the baseline
migration.** Leads with `CreateExtension("postgis")`, then creates `User`. Closes both A6 red
signals: `pytest` is **96 passed**, `makemigrations --check --dry-run` exits **0**.

- ⚠️ **`CreateExtension("postgis")` must stay the first operation of the first migration.** It lives
  in `identity` — not `geo`/`reporting`, which will own the geometry — because Django orders by the
  dependency graph, not app name, and `identity.0001` is the earliest project-owned node
  (`AUTH_USER_MODEL` points at it). A geometry-bearing app must still name it in `dependencies` if
  it has no other path to it.
- ⚠️ **`postgis/postgis` pre-creates the extension, so `migrate` against the compose DB cannot fail
  and proves nothing.** `sqlmigrate` shows the operation as `-- (no-op)` there. Verified instead
  against a fresh `CREATE DATABASE` holding only `plpgsql`; a `geography(Point,4326)` column then
  worked. Re-verify only on an empty database.
- ⚠️ **`identity/0001` is frozen** — it has been applied. It was hand-edited before that (the
  documented posture: a generated migration is a draft, DevOps §7). Reversing it DROPs the
  extension; deployment is forward-only via a pre-deploy Job.

✅ Built in T1.2 (2026-08-05): registration + channel verification. `VerificationCode` + `Channel`
in `identity/models.py`, migration `0002`, `register_citizen`/`send_verification_code`/`verify_code`
services, `identity/serializers.py`, `identity/views.py`, `POST /auth/register` + `POST /auth/verify`,
read-only `VerificationCodeAdmin`, 22 tests. `pytest` **205 passed / 1 xfailed**, mypy/ruff clean.

- ⚠️ **Code delivery is NOT wired up — ❓Q5 is open.** Codes are issued and verifiable; nothing
  transmits them. The `transaction.on_commit(...)` enqueue sits as a comment at the exact line it
  belongs on in `send_verification_code()`. Q5's resolution is an insertion, not a redesign.
- ⚠️ **`verify_code()` must never be decorated `@transaction.atomic`.** The attempt increment has to
  commit *before* the comparison that may reject it, or a wrong code rolls the counter back,
  `MAX_ATTEMPTS` never fires, and a 6-digit code gets unlimited guesses. Two separate `atomic()`
  blocks, `select_for_update()` on the first.
- **The verification code is Argon2-hashed like a password**, and the plaintext is returned as the
  second tuple element rather than an attribute, so passing the row to a serializer or log cannot
  leak it. Never log it, never put it in a response body.
- **`CODE_LENGTH=6`, `TTL=10min`, `MAX_ATTEMPTS=5` are our policy, not spec-derived** — API §6.1
  fixes only the shape and the expired-code `422`.
- **Pre-session `/auth/verify` returns one generic message for every failure** (no enumeration
  oracle); an authenticated caller gets the specific reason. A test asserts the unknown-address and
  wrong-code replies are identical.
- **`VerificationCodeAdmin` is fully read-only and never surfaces `code_hash`** — a writable
  `attempts` or `consumed_at` would defeat the service's controls.
- **Verification failure is always an exception, never a `False` return** — a bool invites
  `if verify_code(...)` with no `else`, which fails open.

✅ Built in T1.3 (2026-08-07): sessions, login, logout, revocation. `authenticate_user` /
`start_session` / `end_session` / `revoke_all_sessions` in `identity/services.py`;
`Login`/`LoginResponse` serializers; `LoginView` + `LogoutView`; `POST /auth/login` +
`POST /auth/logout`; `InvalidCredentials` + `AccountLocked` in `api/exceptions.py`; 28 tests.
**No migration.** `pytest` **233 passed / 1 xfailed**, mypy (109 files) / ruff clean.

- ⚠️ **Revoke through `SessionStore(session_key=...).delete()` — never
  `Session.objects.filter(...).delete()`.** On `cached_db` a raw row delete leaves the cached copy
  live and the session keeps authenticating until the cache expires. The ORM call looks correct and
  the row really does vanish, which is what makes it a silent hole. Applies to every future caller:
  BR-25 suspension and BR-33 deprovisioning both revoke this way.
- **`start_session()` wraps `django.contrib.auth.login()`** for its `cycle_key()` — the
  session-fixation defence. A hand-rolled `request.session[SESSION_KEY] = ...` sets the same keys
  and looks equivalent while leaving a planted pre-login token valid. `backend=` must be passed
  explicitly since the user comes from a service, not `authenticate()`.
- ⚠️ **Password is checked BEFORE account status**, and `AccountLockedError` subclasses
  `AuthenticationError` (fail-closed for `except AuthenticationError`). The view's `except` clauses
  are ordered locked-first — reversing them silently turns every `403 ACCOUNT_LOCKED` into a `401`.
- **`authenticate_user()` hashes a throwaway password when the identifier is unknown**, or response
  timing becomes the enumeration oracle the identical error messages exist to prevent.
- ⚠️ **DRF's `NotAuthenticated` → `403` rewrite is undone in `urbenmend_exception_handler`.**
  `SessionAuthentication` offers no `WWW-Authenticate` header, so DRF rewrote every unauthenticated
  reply to `403`; API §4.2 requires `401 UNAUTHENTICATED`. Fixed once globally — do not re-add
  per-view `handle_exception` overrides. For the same reason login failures use a plain
  `APIException` (`InvalidCredentials`), not `NotAuthenticated`/`AuthenticationFailed`, which
  `handle_exception` special-cases.
- **`LoginSerializer` validates shape only** — no `EmailField`, no password `min_length`. Both would
  answer a question the caller has not earned: that their guess was not even a valid address, or
  what the password policy is. `trim_whitespace=False` — a leading space is part of the secret.
- ⚠️ **`requires2fa` ships hardcoded `False`.** When T1.7 makes it a real check, `LoginView` must
  simultaneously stop issuing a full session — the spec puts `/auth/2fa/verify` on a *partial*
  post-password session.

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
