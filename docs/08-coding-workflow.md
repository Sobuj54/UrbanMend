# UrbanMend — Coding Workflow (Execution Playbook)

|                      |                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| **Document**         | `docs/08-coding-workflow.md`                                                                      |
| **Version**          | 2.0 (retargeted to Python + Django + DRF per ADR-001; expanded to a step-by-step playbook)          |
| **Applies after**    | Design phase complete — docs 01–07 approved                                                        |
| **Scope**            | *How* to build. The *what* and *in what order* live in [05-project-plan.md](05-project-plan.md).    |
| **Stack**            | Python + Django + DRF ([07-adr-001-app-framework.md](07-adr-001-app-framework.md), Accepted)        |

## How to use this document

Part A runs **once** to stand up the skeleton (phase P0). Part B is the **loop you repeat for every
task** in the plan, from T1.1 to T10.8. Parts C–H are the reference material that loop leans on.

Two rules that override everything else here:

1. **The approved docs are the source of truth.** If your code needs to differ from
   [04-api-specification.md](04-api-specification.md), you amend the spec first, in its own PR
   (DC-3, R-9). Code never wins an argument with the spec by default.
2. **Build only what traces to an ID.** Every commit should map to a task (`T4.4`), a requirement
   (`FR-18`), or a business rule (`BR-26`). If you can't name the ID, you're inventing scope (R-9).

Notation: **[doc]** = a constraint from the planning docs, cited. **[practice]** = ordinary
engineering practice this playbook adds; not doc-mandated, adapt if you have a better way.

---

# Part A — Bootstrap (once, = phase P0 / milestone M0)

Do these in order. The ordering is not cosmetic: step A6 is irreversible if you get it wrong.

## A0. Prerequisites

Install Docker Desktop and git. **Do not install Python, GDAL, GEOS, or PROJ natively on Windows** —
Docker Compose is the *mandated* development environment of record precisely because GeoDjango's
native dependency chain is awkward to install on Windows, this team's platform [doc: DevOps §3.1,
Plan P0 stack note]. Everything below runs inside containers.

## A1. Repo skeleton + git hygiene

```
manage.py
requirements.txt          # or requirements/{base,dev}.txt — your call
.dockerignore             # exclude .git, __pycache__, *.pyc, .venv, test fixtures, local env, media/static output
.env.example              # committed, placeholders only
Dockerfile
docker-compose.yml
docker-compose.override.yml
urbenmend/                # Django project package
```

`.gitignore` already covers `.env.local`, caches, `staticfiles/`, `media/`. Confirm `.env.example`
is *not* ignored — it must be committed [doc: DevOps §3.2].

## A2. Dependencies (T0.1)

Every package below is named somewhere in docs 02/05/06/07. **Pin every line** — an unpinned
transitive dependency defeats the SHA-tagged deployment model [doc: DevOps §2.2]. `pip-compile` or
`uv` for hash-pinning is *preferred*, not mandated.

| Purpose | Package |
| --- | --- |
| Core | `django`, `djangorestframework` |
| Geospatial | `djangorestframework-gis` |
| DB driver | `psycopg` |
| Config | `django-environ` |
| Async | `celery`, `redis` |
| Storage | `django-storages` (+ `boto3`) |
| Images | `Pillow` |
| Auth | `django-otp`, `argon2-cffi` |
| Server | `uvicorn` |
| Observability | `structlog`, `django-prometheus`, OpenTelemetry Django + Celery instrumentation |
| Dev/test | `pytest`, `pytest-django`, `factory_boy`, `ruff`, `mypy` |

✅ **Versions — decided in T0.1 on 2026-08-03.** These were deliberately unpinned in the docs;
they are now settled and verified:

| | Pinned | Why |
| --- | --- | --- |
| Python | **3.13** | Ceiling imposed by `djangorestframework-gis` 1.2.1 (supports 3.9–3.13) |
| Django | **5.2.16 LTS** | Security support to **Apr 2028**; 6.0 ends Apr 2027 |
| DRF | **3.17.1** | Supports Django 5.2/6.0/6.1 |

GeoDjango support was confirmed before pinning, as DevOps §12 requires — verified inside
`python:3.13-slim` with `binutils libproj-dev gdal-bin`: **GEOS 3.13.1, GDAL 3.10.3**, both inside
Django 5.2's supported ranges (GEOS 3.8–3.14, GDAL 3.1–3.11, PROJ 6–9). The `python:3.12-slim` in
the DevOps Dockerfile example is superseded by `python:3.13-slim`.

Pinning workflow: **`pip-compile`** (pip-tools), `requirements/{base,dev}.in` → `.txt` with
`--generate-hashes`. `dev.txt` additionally needs `--allow-unsafe` (pip-tools leaves its own
`pip`/`setuptools` deps unpinned, which pip rejects in a hashed file).

Two traps found while pinning, both recorded in `requirements/dev.in` so they are not "corrected"
back: `django-stubs` versions track the Django release they *fully* support, so DRF-stubs 3.17
requires `django-stubs>=6.0.4` (**6.0.7**) even though we run Django 5.2 — not `~=5.2`. And
`types-redis` is obsolete: `redis` ships its own annotations since 5.0.

## A3. Docker Compose + Dockerfile (T0.2)

Five services [doc: DevOps §3.1]: `db` (**`postgis/postgis`** — not plain `postgres`, the extension
must exist before the first migration enables it), `redis`, `storage` (MinIO), `api`, `worker`.

Dockerfile rules, all mandated [doc: DevOps §2.2]:

- Multi-stage, `python:3.x-slim` base — **not Alpine** (GDAL/GEOS/PROJ and `psycopg` have no musl wheels)
- **Non-root user** in the final stage
- One image for both processes; the process is selected by the container `command`, not by a second Dockerfile
- ⚠️ **Never run `migrate` in the Dockerfile or the entrypoint.** It is a separate pre-deploy step
- `collectstatic` runs at build time with settings that don't require `SECRET_KEY`/`DATABASE_URL` —
  no secrets exist at build time

⚠️ **Runtime library package names in the DevOps §2.2 example are Debian 12 (bookworm) and are stale.**
`python:3.13-slim` is Debian 13 (trixie), where the correct names are **`libgdal36`** (not `libgdal32`)
and **`libgeos-c1t64`** (not `libgeos-c1v5`); `libproj25`, `libpq5`, and `gdal-bin` are unchanged.
Verified in-image: GEOS 3.13.1, GDAL 3.10.3 load successfully. Re-check with
`apt-cache search --names-only 'libgdal[0-9]'` if the base image's Debian release changes.

A **third `dev` stage** (after `runtime`) installs `requirements/dev.txt` for Compose and CI. It is
deliberately ordered last so `--target runtime` — the deployed image — can never pick up
pytest/ruff/mypy. Compose's `docker-compose.override.yml` targets `dev`; production targets `runtime`.

Verified locally: `db` reports **PostgreSQL 17.5 / PostGIS 3.5.2** and the app role can
`CREATE EXTENSION postgis` (A7's prerequisite); `redis` and `storage` (MinIO) both pass healthchecks.

## A4. Settings + config (T0.3)

Split settings loaded through `django-environ` so identical variable names work locally and deployed.

⚠️ **Naming conflict — resolved.** Plan T0.3 said `base`/`dev`/`prod`; DevOps §3.2 said
`DJANGO_SETTINGS_MODULE=urbenmend.settings.local`. **`base`/`dev`/`prod` wins**; DevOps §3.2 was
amended to match. Do not reintroduce `settings.local`.

`.env.local` (git-ignored) mirrors `.env.example` (committed). The full local set [doc: DevOps §3.2]:

```
DJANGO_SETTINGS_MODULE=urbenmend.settings.dev
DJANGO_SECRET_KEY=<local-dev-secret>
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DATABASE_URL=postgis://user:pass@db:5432/urbenmend
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
STORAGE_ENDPOINT=http://storage:9000
STORAGE_BUCKET=urbenmend-media
LLM_API_KEY=<your-key>
```

⚠️ The scheme is **`postgis://`**, not `postgres://` — that is what selects the
`django.contrib.gis.db.backends.postgis` engine when the URL is parsed. Getting this wrong produces a
confusing "unknown field type" error much later. If GDAL/GEOS aren't found, set `GDAL_LIBRARY_PATH` /
`GEOS_LIBRARY_PATH` explicitly.

✅ **Built and verified (2026-08-03).** `urbenmend/settings/{__init__,base,dev,prod,build}.py`, plus
`urbenmend/{urls,asgi,wsgi}.py` (all three are imported by `manage.py check` or the container command,
so they had to land here) and `pyproject.toml` (ruff/mypy/pytest config — CI and a dev shell must read
identical settings).

**`build.py` is a fourth module the plan did not name.** The Dockerfile's `collectstatic` step needs a
settings module that imports with no secrets present [doc: DevOps §2.2]. It `os.environ.setdefault`s
throwaway values for `DJANGO_SECRET_KEY` and `DATABASE_URL` *before* importing `base`, so `base` can
keep both **required with no fallback** — a missing secret in a deployed environment fails startup
instead of silently running on a known key. `prod.py` has no fallbacks at all.

Two decisions this step forced, neither pre-answered by the plan:

1. **Apps nest under `urbenmend.`, not the repo root.** A top-level `platform/` package would shadow
   the stdlib `platform` module that Django itself imports — an import-order failure that is painful
   to diagnose. Arch §2.4's app *names* are unchanged; only the import path is qualified. A5 creates
   them as `urbenmend/identity/`, `urbenmend/platform/`, etc.
2. **`DEFAULT_PAGINATION_CLASS` is deliberately left unset**, with `rest_framework.W001` in
   `SILENCED_SYSTEM_CHECKS`. Pagination is mandatory (NFR-2), but no DRF built-in emits the required
   `{data, page, meta}` envelope with opaque cursors (API §1.3/§4.4). Naming a built-in as a
   "placeholder" would ship the wrong contract on every list view the moment one exists. T0.6 adds the
   custom class and removes the silencer.

Also settled here: `MEDIA_ROOT = mediafiles/` (**not** `media/`, which is an app name — the A1
`.gitignore` fix), `STORAGES` dict instead of the `DEFAULT_FILE_STORAGE`/`STATICFILES_STORAGE` pair
removed in Django 6.0, static files on local disk (build-time `collectstatic` has no credentials to
upload with), and `OTPMiddleware` placed after `AuthenticationMiddleware` from the start so 2FA (FR-4)
is not retrofitted.

Verified in the container: `collectstatic` under `settings.build` copies 162 admin/DRF assets;
`manage.py check` clean on `dev`; **`manage.py check --deploy` clean on `prod`** (the A9 gate);
`ruff check`, `ruff format --check`, and `mypy --strict` all pass; a `postgres://` URL raises the
fail-fast `ValueError`; structlog *and* stdlib `logging` records both render as JSON on one stream
(T0.9); Redis cache round-trips and the `cached_db` session backend loads against the live services;
GeoDjango constructs `POINT (90.4125 23.8103)` at SRID 4326; and uvicorn boots
`urbenmend.asgi:application` and serves HTTP.

## A5. App skeleton (T0.1)

One Django app per architecture module [doc: Arch §2.4]:

```
identity  reporting  media  classification  issues  geo
notifications  moderation  audit  export  platform
```

⚠️ **Created as `urbenmend/<app>/`, imported as `urbenmend.<app>`** — decided in A4. A root-level
`platform/` package shadows the stdlib `platform` module that Django imports. Names are unchanged.

`platform` holds cross-cutting concerns (outbox, base classes, middleware). Dashboard/query needs no
app — it is served by `issues`/`geo` selectors.

**Create `services.py` and `selectors.py` in every app on day one, even empty.** This is R-12's
named mitigation: the risk is that "service-layer discipline erodes under Django's idiom, scattering
authorization into views/serializers", and the countermeasure is the convention existing from the
start so there is never a moment where putting logic in a view is the path of least resistance.

✅ **Built and verified (2026-08-03).** All 11 apps exist under `urbenmend/`, each carrying
`__init__.py`, `apps.py`, `models.py`, `services.py`, `selectors.py`, `admin.py`, `migrations/` and
`tests/` — 97 Python files, no app missing a file.

`startapp` was **not** used. It emits an `AppConfig` whose `name` is the bare label (wrong here — the
apps import as `urbenmend.<label>`), omits `services.py`/`selectors.py` entirely, and adds a
`tests.py` that collides with the `tests/` package. The tree was written directly by a throwaway
script, then deleted.

Each `AppConfig` sets an explicit `label` so the short name survives the dotted path, and a
`verbose_name` matching the Arch §3 module name — that is what Django admin shows, and FR-30/31
surface reference data and moderation through admin. The docstring headers in `services.py` and
`selectors.py` carry the layering rules (authorize before mutating; `transaction.atomic` for
multi-write; enqueue via `transaction.on_commit`; selectors never write and apply the caller's
visibility rules) so the constraint is visible where the code gets written, not only in this doc.

**The conventions are now enforced by a test**, not just documented:
`urbenmend/platform/tests/test_app_skeleton.py` asserts the app set matches Arch §2.4 exactly, that
every app imports as `urbenmend.<label>`, that all five layering modules import in each app, and that
each app owns a migrations package — 68 cases. This also stops `pytest` exiting 5 ("no tests ran"),
which would otherwise fail the CI test stage (A9) for a reason unrelated to code quality.

Verified in the container: `manage.py check` clean, `makemigrations --check --dry-run` reports no
changes (exit 0), `ruff check`, `ruff format --check`, `mypy --strict` (87 files) and `pytest`
(68 passed) all pass. Django resolves all 11 labels to their `urbenmend.*` paths.

## A6. ⚠️ Custom user model — before the first migration (T0.10 / T1.1)

**Declare `AUTH_USER_MODEL` and create the `identity` user model before you run `migrate` even
once.** This is irreversible afterwards [doc: Plan T0.10, Arch §2.4]. Recovering means dropping the
database and starting over.

The model carries an explicit `role` field (Citizen/Authority/Admin) plus an authority↔category
scope relation. **Do not use `django.contrib.auth` Groups/Permissions for RBAC** — they cannot
express BR-26 category scoping [doc: Arch §2.4, Plan T1.5].

✅ **Built and verified (2026-08-04).** `AUTH_USER_MODEL = "identity.User"` is set in
`settings/base.py`; `urbenmend/identity/models.py` holds `User`, `UserManager`, and the `Role` /
`UserStatus` / `Language` enums; `admin.py` registers it and `tests/test_models.py` covers it in
28 cases.

`User(AbstractBaseUser, PermissionsMixin)` with a **UUID PK** (API §1.2 forbids guessable IDs in
URLs — a sequential integer leaks the user count and enables enumeration). Choices that were
judgement calls rather than doc-derived, with the reasoning, so a later reader does not re-litigate
them:

- **`email` and `phone` are both nullable and both UNIQUE**, with a `CheckConstraint`
  (`identity_user_has_contact_or_anonymized`) requiring at least one. Absence is `NULL`, never `""` —
  Postgres permits many NULLs under a UNIQUE index but only one empty string, so `""` would let the
  second contactless account collide with the first.
- **The constraint has a `status=deleted` escape hatch.** Without it, the anonymization that
  `DELETE /users/me` requires (P6, BR-33, C-14) would be impossible: clearing PII while retaining the
  row is exactly the state the constraint would otherwise reject.
- **Verification is two timestamps, not two booleans.** The API's `verified: {email, phone}`
  (API §6.2) is derivable from timestamps; the reverse loses *when* verification happened, which the
  T2 trust signal and FR-32 audit both need.
- **`is_active` is a derived read-only property**, not a column, so it can never contradict `status`.
  `registered` counts as active (an unverified account may sign in with limited capability, BR-30).
  Assignment raises `AttributeError` on purpose — callers must move `status` (T1.9). mypy flags the
  narrowing as unsound (`AbstractBaseUser.is_active` is writable); the `type: ignore[override]` is
  deliberate and commented.
- **Contact normalization runs in `save()`, not only `clean()`** — DRF serializers never call
  `full_clean()`, so `clean()` alone would leave the API path unnormalized. `clean()` also skips
  `super()`: `AbstractBaseUser.clean()` raises `TypeError` when `USERNAME_FIELD` is `None`, which is
  legitimate here for phone-only accounts.
- **`PermissionsMixin` is inherited for admin plumbing only** — admin needs `is_staff`,
  `is_superuser`, `has_perm()` to function, and FR-30/31 surface moderation through admin. ⚠️ Domain
  RBAC must never live in `groups`/`user_permissions`; a test asserts the two stay independent.

Two fields are **deliberately absent**, both commented in the model: the Authority↔Category M2M
(Category is T0.10 baseline-schema scope, and adding the M2M later is an ordinary additive migration
with none of this file's irreversibility — until then no Authority can be scoped, so no scoped read
passes, which is the safe direction to fail) and the T1/T2 trust signal (computable from
`date_joined` plus the verification timestamps; storing a score would invent a weighting the docs do
not specify, and FR-21 already removed the one tunable numeric score).

`BaseUserAdmin` needed two adjustments: `add_fieldsets` was replaced because the default names
`username`, which does not exist here, and the class is subscripted only under `TYPE_CHECKING` —
django-stubs types it as generic but the runtime class is not subscriptable, so `BaseUserAdmin[User]`
in a base-class list raises `TypeError` during admin autodiscovery.

Verified in the container: `manage.py check` clean (1 silenced), `ruff check`, `ruff format --check`
and `mypy --strict` (88 files) all pass.

⚠️ **`pytest` ends A6 at 79 passed / 17 errored, and `makemigrations --check --dry-run` exits 1.
Both are the correct A6 end state, not defects.** `django.contrib.admin`'s migration now depends on
`('identity', '__first__')`, so every database-backed test errors with *"Dependency on app with no
migrations: identity"* until A7 creates it. The 17 were confirmed green against a throwaway
`makemigrations identity` (28/28 passed against real PostGIS — CheckConstraint, the
case-insensitive UNIQUE collision and the anonymization path all hold), and that migration was then
deleted so **A7 owns the real `0001`, which must lead with `CreateExtension('postgis')`**.

Two tooling exemptions were added to `pyproject.toml`: `django_otp.*` gets
`ignore_missing_imports` (no `py.typed`; nothing here imports it — the django-stubs plugin pulls it
in because `otp_totp` is in `INSTALLED_APPS`, so the error lands on an unrelated file), and
`**/tests/*.py` gets `S105`/`S106` (a test that hashes a password has to name one; application code
is still checked).

## A7. First migration enables PostGIS (T0.4)

The very first migration runs `CreateExtension('postgis')` before any geometry column exists
[doc: Arch §2.3, Plan T0.4]. Verify from zero:

```bash
docker compose run --rm api python manage.py migrate
```

✅ **Built and verified (2026-08-05).** `urbenmend/identity/migrations/0001_initial.py` leads with
`CreateExtension("postgis")`, then creates `User`. 22 migrations apply from an empty database;
**`pytest` is now 96 passed** (the 79 + 17 A6 predicted) and **`makemigrations --check --dry-run`
exits 0** — both A6 red signals are closed.

**The extension operation lives in `identity`, not in `geo`/`reporting`** — the apps that will
actually own geometry. Those migrations do not exist yet, and Django orders by the dependency graph,
not by app name, so there is no "first app alphabetically" to rely on. `identity.0001` is the
earliest project-owned node in that graph: `AUTH_USER_MODEL` points at it, so `contrib.admin`,
`otp_totp`, and every future model with an FK to the user depend on it transitively.
⚠️ A geometry-bearing app must still name this migration in its `dependencies` if it has no other
path to it — do not assume app-registry order will save you.

⚠️ **`postgis/postgis` pre-creates the extension in its initdb scripts, so the obvious check proves
nothing.** On the dev database `sqlmigrate identity 0001` renders the operation as literally
`-- (no-op)`: Django probes `pg_extension` first and emits `CREATE EXTENSION IF NOT EXISTS` only
when needed. A green `migrate` against the compose database is therefore *not* evidence the
operation works. It was verified against a database created with `CREATE DATABASE` and confirmed to
hold only `plpgsql`:

```bash
docker compose exec -T db psql -U urbenmend -d postgres -c "CREATE DATABASE a7_probe;"
docker compose run --rm -e DATABASE_URL="postgis://urbenmend:urbenmend@db:5432/a7_probe" \
  api python manage.py migrate
docker compose exec -T db psql -U urbenmend -d a7_probe -c "SELECT extname FROM pg_extension;"
```

`postgis` was present afterwards, and a `geography(Point,4326)` column then accepted
`POINT(90.4125 23.8103)` — the shape the Report and POI columns will use. The probe database was
dropped. Anyone re-verifying this must use an empty database; the compose one cannot show a failure.

Schema confirmed in Postgres: UUID PK, `email`/`phone` both nullable with UNIQUE constraints, the
`identity_user_has_contact_or_anonymized` CHECK (rendered
`email IS NOT NULL OR phone IS NOT NULL OR status::text = 'deleted'::text`), the
`(role, status)` index, and `otp_totp_totpdevice` / `django_admin_log` FKs resolving to
`identity_user` — proof `AUTH_USER_MODEL` took effect rather than silently falling back.

⚠️ **The reverse operation DROPs the extension**, which on a database holding geometry data is
destructive. Forward-only in deployment: migrations run as a pre-deploy Job, never rolled back
in place [doc: DevOps §7].

The generated migration was **hand-edited before first application** — `makemigrations` does not
know about the extension, so a reviewed edit is the only way it leads. That is the documented
posture (a generated migration is "a draft to be reviewed, not an artifact to be trusted",
DevOps §7) and is safe only because this migration had never been applied to a shared environment.
⚠️ It is frozen now.

## A8. Base API + Worker + observability (T0.6–T0.9)

- **API base (T0.6)** — `/api/v1` routing; a **custom DRF exception handler** emitting the §4.1 error
  envelope; a **custom pagination class** emitting `{data, page, meta}` with opaque cursors. Both are
  required because DRF's defaults differ from the contract [doc: API §1.3/§4.1, Plan T0.6]. Add the
  `camelCase` renaming layer here too — DRF serializers emit `snake_case` by default, which the API
  spec calls "the single easiest way for the implementation to silently drift from the contract".
- **Worker base (T0.7)** — Celery app sharing Django settings; same image, different entrypoint.
  ⚠️ `-B` runs beat in-process, which is fine locally; in deployment **beat is a separate Deployment
  pinned to exactly one replica and never autoscaled** — two schedulers double-fire the outbox relay
  [doc: DevOps §3.1/§6.1].
- **Health (T0.8)** — `GET /health` with per-dependency degradation flags [doc: API §6.16, NFR-4].
- **Logging (T0.9)** — `structlog`, JSON to stdout/stderr (**never to files inside the container**),
  correlation/`traceId` middleware propagated into Celery task headers.

## A9. CI pipeline (T0.5)

Wire all seven stages now, before there is code to break. See Part F.

✅ **Built and verified (2026-08-05).** `.github/workflows/ci.yml` (7 jobs) plus
`.github/actions/dev-image/action.yml`, a composite action the first five jobs reuse.

✅ **The CI vendor is GitHub Actions** — chosen by the user in A9. Every planning doc left it
unpinned, so this is a recorded decision rather than a doc-derived one. The stage list and their
fail conditions come from DevOps §4.1 and are vendor-independent.

Dependency graph (not a straight line — the three cheap gates fan out from `lint` and run
concurrently, which is what keeps the critical path at roughly build+scan time):

```
lint ──┬── drift ────────┐
       ├── deploy-check ─┼── integration ── build ── push-image
       └── unit ─────────┘
```

⚠️ **Every gate runs inside the Dockerfile's `dev` stage, not on the bare runner.** GeoDjango
`dlopen()`s GEOS/GDAL, so `mypy` and `pytest` both fail without them — and the A3 runtime library
names are Debian 13 (trixie) specific, while `ubuntu-latest` is Ubuntu noble where the same
libraries are named `libgdal34`/`libgeos-c1v5`. Hand-installing them on the runner would fork the
dependency set the deployed image actually uses, which is precisely the drift A3 documented. The
buildx GHA cache is keyed on `requirements/*.txt`, so an app-code-only change restores every
dependency layer and rebuilds only the final `COPY`.

Four decisions this step forced, none pre-answered by the docs:

1. **The unit/integration split is by pytest MARKER, not by directory.** `testing.md` explicitly
   records that this split is unspecified ("both stages are literally `pytest`") and warns against
   inventing a test layout. Stage 4 therefore runs `pytest -m "not django_db"` — verified as
   **166 passed, 17 deselected** — and stage 5 runs the full suite against real services. No
   files moved, and the marker is already on every DB-backed test.
2. **Stage 3 uses a throwaway 50-character `DJANGO_SECRET_KEY` inline, not a repository secret.**
   `check --deploy` opens no connection and signs nothing, so a real secret would be a production
   credential sitting in a public build log for no benefit. The key is long and non-repeating
   because Django's own `security.W009` fails a short or low-entropy one — and that warning would
   otherwise mask a genuine finding. `--fail-level WARNING` makes any Django security warning fatal.
3. **The CI database role is a superuser named `ci`, deliberately *not* the application role.**
   `identity/0001` must `CREATE EXTENSION postgis` against a database built from zero. `testing.md`
   requires the two roles differ: T8.1 enforces the audit tables' append-only rule by REVOKEing
   `UPDATE`/`DELETE` from the *application* role, and if CI ran as that role the revoke script
   itself would be untestable.
4. **Trivy runs with `ignore-unfixed`.** A CVE with no available patch cannot be actioned by this
   pipeline; without the flag an upstream Debian lag would block every merge, and a permanently red
   gate stops being read. `CRITICAL`/`HIGH` with a fix available still fail the build.

Also settled here: stage 5 runs migrations **from zero and then back down** (`migrate identity zero`)
as a step separate from `pytest`, because pytest builds its test database with `migrate` but never
exercises the reverse direction — the reversibility gate `testing.md` asks for would otherwise be
unchecked. ⚠️ That reverse DROPs the postgis extension, which is safe *only* because the probe
database is thrown away with the runner; deployment stays forward-only via a pre-deploy Job
(DevOps §7). Stage 6 builds `--target runtime`, never `dev`, so the scanned and pushed artifact
cannot carry pytest/ruff/mypy. Stage 7 is gated on a push to `main`/`staging` and is the only job
granted `packages: write` — a fork's PR must never reach registry credentials — and it tags the
image with the commit SHA only, never `latest` (DevOps §2.3).

Verified: both YAML files parse and the `needs:` graph resolves in the order above; the stage-4
marker filter deselects exactly the 17 DB-backed tests; the nested heredoc in the migration step
survives YAML block-scalar de-indentation and executes (checked by running the parsed string).
⚠️ The workflow has **not** been executed on GitHub — that needs a push, and stages 5–7 need the
runner's service containers and `GITHUB_TOKEN`. The first push is where a service-container
hostname or a registry permission would surface.

## A10. Write one failing test on purpose

Before leaving P0, write the **P4 clustering-concurrency test as a failing test** [doc: Plan §8.1].
R-2 (duplicate Issues under concurrent submission) is the single most expensive defect to discover
late, and a red test sitting in the suite from P0 is what stops it shipping.

✅ **Built and verified (2026-08-05)** — `urbenmend/issues/tests/test_clustering_concurrency.py`.
One test, `xfail(strict=True)`, currently **XFAIL** with the rest of the suite green
(183 passed, 1 xfailed).

**What it asserts.** Two distinct citizens submit reports of the same real-world issue at the same
coordinate in the same category, and `cluster_report()` is called for both **in parallel**
(`ThreadPoolExecutor`, `django_db(transaction=True)` so each thread holds a real connection and a
real transaction — which is what makes the advisory lock observable). Three assertions:
both calls return the same Issue id; `Issue.objects.count() == 1`; and both Reports are attached.
The third matters — a lock that serialises correctly but drops the second attachment would
satisfy "one Issue" while losing a citizen's submission.

**Four judgment calls:**

- **`xfail(strict=True)`, not a bare failing assert.** A permanently-red test fails CI stage 5,
  which gates stages 6–7 — no image would build for the whole of P1–P3, and an always-red gate
  stops being read (the same reasoning behind Trivy's `ignore-unfixed`). `strict=True` keeps the
  forcing function intact from the other side: an **unexpected pass is a failure**, so when T4.4
  lands this test cannot go quietly green. Someone has to delete the marker deliberately.
  `skip` was rejected outright — it would not run at all.
- **It calls the future `cluster_report()` rather than inlining a find-or-create.** The first
  draft reimplemented the query-then-insert inside the test body; that only ever tests itself,
  and would still pass in P4 while the real service raced. The seam asserted is the one T4.4
  must implement.
- **No radius is asserted.** Radius and time-window are per-category reference data
  (ASSUMP-4, NFR-11, Arch §4.3/§349 "not hard-coded"). The draft's hard-coded 50 m would have
  contradicted that; identical coordinates are inside *any* conservative radius, so the test
  cannot become tuning-dependent.
- **`type: ignore[attr-defined]` on the four not-yet-existent imports.** `strict = true` implies
  `warn_unused_ignores`, so mypy errors on those very lines the moment T4.4 defines the names —
  the same self-cleaning property as the strict xfail, applied to the type checker.

The three `_seed_*` helpers now delegate to the `factory_boy` factories delivered by T2.1/T4.1;
their call sites and the concurrency assertions remain unchanged.

⚠️ **The test has never actually exercised a lock** — it XFAILs at the first import. It pins the
contract, not the implementation. T4.4 is where it first runs for real, and a green run there is
only meaningful if the marker is removed in the same commit.

## A11. DC-1 — operations documentation

The M0 gate's documentation deliverable [doc: Plan §8.2]: environment/setup notes, runbook
skeleton, migration guide.

✅ **Written and verified (2026-08-05)** — [09-operations.md](09-operations.md). Three sections
matching the checkpoint's three deliverables, plus an open-questions table.

⚠️ **§2 (runbook) is explicitly a skeleton, and says so at the top.** No environment has been
deployed, so subsections for deploy, rollback, backup/restore and on-call are marked **(DC-6)** and
state the *questions they must answer* rather than procedures. Writing them out would have been
fiction, and a runbook that reads as verified but was never executed is worse than an obvious gap.
DC-6 (end of P10) completes them. §2.9 is the exception — it condenses DevOps §9.1, which already
specifies the common operations, and is labelled unrehearsed rather than unwritten.

§1 and §3 describe what actually exists. Every documented command was executed against the running
stack before the doc was committed, which caught a real error in my own draft: the §1.6 line
`docker compose exec api ruff check && ruff format --check` runs the second command **on the host**,
where ruff is not installed. Corrected to one command per line, with the `sh -c '...'` form noted
for chaining.

Verified: ruff clean, mypy clean (105 files), `pytest` 183 passed / 1 xfailed,
`makemigrations --check` "No changes detected", `migrate` idempotent on re-run, and
`GET /api/v1/health` → `200 {"status":"ok","dependencies":{"database":{"status":"ok"},"cache":{"status":"ok"}}}`.
`.env.local` confirmed git-ignored (`.gitignore:25`); only `.env.example` is tracked.

## M0 gate — do not start P1 until all of these hold
- [ ] CI is green on all stages — ⚠️ **workflow authored but never executed on GitHub** (A9)
- [x] API and Worker both boot
- [x] Migrations apply cleanly **from zero**
- [x] `/health` reports each dependency's state
- [x] A trivial round-trip request returns the standard error envelope on failure
- [x] Python/Django/DRF versions pinned; settings-module naming conflict resolved
- [x] DC-1 written: environment/setup notes + runbook skeleton + migration guide

---

# Part B — The per-task loop (repeat for every task T1.1 → T10.8)

This is the loop you run for every task in [05-project-plan.md](05-project-plan.md). Each iteration
is one branch, one PR.

## B1. Before you write a line of code

1. **Read the task entry in the plan.** Note its `Dep` column — all dependencies must be merged
   before you start. Note the `Traces` column — those are the IDs your commit messages will cite.
2. **Read the relevant spec sections.** For any task touching an endpoint, open
   [04-api-specification.md](04-api-specification.md) and read the full endpoint contract including
   auth requirements, request/response shapes, error codes, and edge cases. Do this *before* writing
   the serializer, not after.
3. **Check for open questions.** If the task references a `❓Qx` that is still open, stop. The plan
   lists it as a decision gate. Resolve the question (update the doc) before building the feature.
   Only ❓Q10 (accuracy bar / confidence thresholds) remains open as of plan v1.2.
4. **Create the branch.** `git checkout -b feature/<task-id>-<slug>` — e.g.
   `feature/T1.3-sessions`. One task per branch; one concern per commit.

## B2. The vertical slice order

Build in this order every time. Skipping steps or doing them out of order is how logic ends up in
the wrong layer:

```
1. Model (models.py)
2. Migration (makemigrations, review the SQL, commit)
3. Selectors (selectors.py — read queries, no side effects)
4. Services (services.py — writes, RBAC checks, transactions, domain events)
5. Serializer (serializers.py — shape only; camelCase renaming layer here)
6. View + URL (views.py / urls.py — thin; delegates to service/selector)
7. Unit tests (fast, no DB)
8. Integration tests (real DB + Redis)
9. Contract test assertion (does the response match the spec?)
```

**Where logic lives:**

| Concern | Where |
| --- | --- |
| Business rules, RBAC checks, transactions | `services.py` |
| Read queries, filtering, ordering | `selectors.py` |
| Request/response shape, camelCase | `serializers.py` |
| HTTP routing, auth guard, pagination | `views.py` / `urls.py` |
| Domain events, outbox writes | `services.py` (inside the same transaction) |
| Task enqueue | `services.py` via `transaction.on_commit` |

**Never put business logic or RBAC checks in a view, serializer, or DRF permission class.** DRF
permission classes are defence-in-depth, not the enforcement point (FR-3, R-12). A permission class
that returns `False` is a safety net; the service must already have rejected the call.

## B3. Model + migration

- Add the model to the app's `models.py`. Geometry columns use
  `PointField(geography=True, srid=4326)` [doc: Plan T4.2].
- Run `python manage.py makemigrations <app>`. **Read the generated SQL** before committing —
  `sqlmigrate` shows it. Confirm it is backward-compatible (no column drops, no renames in one step).
- Add GiST indexes for every geometry column you query spatially: `Meta.indexes =
  [Index(fields=[...]), GistIndex(fields=['location'])]`.
- Commit the migration file alongside the model change. They are one atomic unit.

⚠️ **Never edit a migration that has already been applied to a shared environment.** If you need to
fix it, write a new migration [doc: DevOps §7].

## B4. Selectors

`selectors.py` contains only read functions — no writes, no side effects, no RBAC enforcement (RBAC
is in the service). Each function takes explicit arguments (no request objects) and returns a
queryset or value. This makes them trivially unit-testable.

```python
# Good
def get_issues_for_authority(*, authority_user, category_ids, filters):
    return Issue.objects.filter(category_id__in=category_ids, ...).order_by(...)

# Bad — request object leaks HTTP concerns into the data layer
def get_issues(request):
    ...
```

## B5. Services

`services.py` contains all writes, all RBAC checks, and all transaction boundaries. Rules:

- Every mutating function is wrapped in `@transaction.atomic` or calls `with transaction.atomic()`.
- Task enqueue always uses `transaction.on_commit(lambda: my_task.delay(...))` — never
  `my_task.delay()` directly [doc: Plan T2.2, Arch §4.1].
- Outbox events are written *inside* the same transaction as the state change [doc: Plan T6.1].
- RBAC check is the first thing in every service function that touches a protected resource. Raise
  `PermissionDenied` (maps to `403`) before touching the DB.
- For clustering find-or-create: acquire a Postgres advisory lock keyed on geohash-cell + category
  inside the atomic block [doc: Plan T4.4, Arch §4.3].

## B6. Serializer

- Rename every `snake_case` field to `camelCase` explicitly. DRF emits `snake_case` by default;
  the API contract is `camelCase`. This is the single most common silent drift point [doc: API §1.2].
- The serializer does not enforce RBAC. It shapes data only.
- For collection endpoints, the custom pagination class (built in A8) wraps output in
  `{data, page, meta}` automatically — don't re-wrap manually.
- For map endpoints, use `GeoFeatureModelSerializer` from `djangorestframework-gis`.

## B7. View + URL

Views are thin. The pattern is:

```python
class IssueStatusView(APIView):
    permission_classes = [IsAuthenticated, IsAuthority]  # defence-in-depth only

    def patch(self, request, pk):
        issue = get_object_or_404(Issue, pk=pk)
        result = issue_service.transition_status(
            issue=issue,
            actor=request.user,
            new_status=request.data['status'],
            reason=request.data.get('reason'),
        )
        return Response(IssueSerializer(result).data)
```

The service raises `PermissionDenied`, `ValidationError`, or a domain exception; the custom
exception handler (T0.6) converts these to the §4.1 error envelope automatically.

Register the URL under `/api/v1/` in the app's `urls.py`, included from the project `urls.py`.

## B8. Tests

Write tests in the same PR as the feature. Three layers:

**Unit tests** — no DB, no network. Mock the service layer. Test selectors with `pytest-django`'s
`db` fixture only when the query logic is non-trivial. Use `factory_boy` factories for all model
instances.

**Integration tests** — real PostGIS + Redis. Test the full HTTP path: request in, response out,
DB state after. Assert:
- Happy path response shape matches the spec (field names, types, envelope).
- Auth/RBAC: unauthenticated → `401`, wrong role → `403`, out-of-scope → `403`/`404`.
- Validation errors return `400`/`422` with the `{error: {code, details}}` envelope.
- State-machine violations return `409 INVALID_TRANSITION`.
- Edge cases named in the spec (out-of-city `422 OUT_OF_CITY`, duplicate confirmation
  `409 ALREADY_CONFIRMED`, etc.).

**Contract assertion** — at minimum, assert the response JSON matches the shape documented in
[04-api-specification.md](04-api-specification.md) for this endpoint. A divergence here is a defect
in the implementation, not the spec.

For concurrency-sensitive tasks (T4.4 clustering, T2.3 idempotency, T5.6/T5.7 merge/split): write
a test that fires two concurrent requests and asserts no duplicate is created. This is R-2's
mitigation.

## B9. Self-review checklist before opening the PR

- [ ] `ruff check && ruff format --check` — clean
- [ ] `mypy` — no new errors
- [ ] `python manage.py makemigrations --check --dry-run` — no drift
- [ ] All new tests pass; no existing tests broken
- [ ] Every response field is `camelCase`; collection responses use `{data, page, meta}`
- [ ] Error responses use `{error: {code, message, details, traceId}}`
- [ ] RBAC check is in `services.py`, not in the view or serializer
- [ ] Task enqueue uses `transaction.on_commit`
- [ ] No secrets, no `DEBUG=True`, no `print()` left in
- [ ] Commit message cites the task ID and requirement ID: `feat(T2.2): async report submission (FR-5, NFR-3)`
- [ ] If the implementation differs from the spec in any way: spec PR opened first

## B10. PR + merge

- PR title: `[T2.2] Async report submission (FR-5, NFR-3)`
- PR description: what changed, what was tested, any open questions or deferred items
- CI must be fully green before merge — all seven stages (see Part F)
- At least one review
- No direct push to `main`; feature branches merge to `staging` via PR [doc: DevOps §1]
- After merge: delete the branch

---

# Part C — Phase-specific notes

These are the non-obvious constraints for each phase. Read the relevant section before starting
that phase. The full task list is in [05-project-plan.md](05-project-plan.md) §5.

## C1. P0 Foundation
Already covered in Part A. The only addition: **do not leave P0 until CI is green and migrations
apply from zero.** Every later phase assumes this.

## C2. P1 Identity & Access
- T1.1: the custom user model is the most irreversible decision in the project. Get it right before
  the first `migrate`. It carries `role` (Citizen/Authority/Admin) and the authority↔category scope
  relation. No `contrib.auth` Groups/Permissions.
- T1.3: sessions on `cached_db`. Test revocation explicitly — delete the session row and assert the
  next request returns `401`.
- T1.5: RBAC is a service-layer concern. Write a test matrix: for each protected endpoint, assert
  that each wrong role gets `403` and each out-of-scope authority gets `403`/`404`.
- T1.8: rate limiting on login/OTP uses DRF throttling backed by Redis. Test that the `429` fires
  and includes `Retry-After`.

### T1.2 — registration + channel verification

✅ **Built and verified (2026-08-05).** `identity/models.py` gains `VerificationCode` and the
`Channel` enum; `migrations/0002_verificationcode.py`; three service functions
(`register_citizen`, `send_verification_code`, `verify_code`); `serializers.py`; `views.py` with
`RegisterView`/`VerifyView`; `POST /auth/register` and `POST /auth/verify` in `api/urls.py`;
`admin.py` gains a read-only `VerificationCodeAdmin`; 22 new tests in
`identity/tests/test_registration.py`.

⚠️ **Delivery is deliberately not wired up — ❓Q5 (notification channels) is open.** The code is
generated, hashed, stored and verifiable, but nothing transmits it. `send_verification_code()`
carries the `transaction.on_commit(...)` enqueue as a comment at the exact line it belongs on, so
Q5's resolution is an insertion rather than a redesign. Registration is therefore complete and
testable end-to-end via the service return value; a user cannot yet receive a code out-of-band.

Six decisions this task forced, none pre-answered by the docs:

1. **The code is hashed with the project's Argon2 hasher, not stored plaintext.** It is a
   credential — the only thing between a stranger who knows an address and a verified account on
   it. Same reasoning and same policy as passwords [doc: auth.md].
2. **`CODE_LENGTH = 6`, `TTL = 10 minutes`, `MAX_ATTEMPTS = 5` are policy, not doc-derived.**
   API §6.1 fixes only the request/response shape and that an expired code yields `422`. Recorded
   here because a reader will otherwise assume a spec citation exists.
3. **⚠️ `verify_code()` is deliberately NOT `@transaction.atomic`.** The attempt counter must
   survive the exception a wrong code raises. Under one enclosing transaction the raise rolls the
   increment back, the counter stays at 0 forever, `MAX_ATTEMPTS` never fires, and an attacker gets
   unlimited guesses at a 6-digit code. Two separate `atomic()` blocks: the first claims an attempt
   and commits, the second spends the code. `select_for_update()` on the first so two concurrent
   attempts cannot both read `attempts=4`.
4. **Failure is always an exception, never a `False` return.** A bool return invites
   `if verify_code(...)` written without an `else`, which fails open.
5. **The pre-session verify path returns one generic message for every failure.** The endpoint is
   unauthenticated, so forwarding the service's specific reason ("already used", "expired", "too
   many attempts") would make it an enumeration oracle — each one confirms the account exists. An
   authenticated caller has already proved who they are, so it gets the detail. A test asserts the
   unknown-address and wrong-code replies are byte-identical in status, code and message.
6. **`VerificationCodeAdmin` is fully read-only and never lists `code_hash`.** A writable
   `attempts` would let anyone with admin reset the brute-force counter; a writable `consumed_at`
   would un-spend a used code. Both defeat the controls the service enforces. One
   `tuple[str, ...]` feeds both `fields` and `readonly_fields` so a field cannot be added to the
   form but left writable — a tuple rather than a list because the two attributes are typed
   differently upstream and only a covariant tuple satisfies both under `mypy --strict`.

Also settled here: the plaintext code is returned as the *second tuple element* rather than an
attribute on the row, so passing the row to a serializer or a log line cannot carry the secret with
it by accident; the verify lookup normalizes the identifier the same way `User.save()` does, or
someone who registered as `Citizen@Example.test` could never verify by typing it back as they wrote
it; and duplicate registration is caught as `IntegrityError` on the UNIQUE index rather than a
pre-check `exists()`, which would race.

Verified in the container: `ruff check` and `ruff format --check` clean, `mypy --strict` clean
(108 files), `pytest` **205 passed / 1 xfailed**, `makemigrations --check --dry-run` exit 0.

### T1.3 — sessions, login, logout, revocation

✅ **Built and verified (2026-08-07).** Four service functions (`authenticate_user`,
`start_session`, `end_session`, `revoke_all_sessions`); `LoginSerializer` +
`LoginResponseSerializer`; `LoginView`/`LogoutView`; `POST /auth/login` and `POST /auth/logout` in
`api/urls.py`; `InvalidCredentials` and `AccountLocked` in `api/exceptions.py` plus a fix to the
handler itself (below); 28 tests in `identity/tests/test_sessions.py`. **No migration** — sessions
live in `django.contrib.sessions`' own table and the user model was unchanged.

⚠️ **The revocation tests are the deliverable, not an extra.** Sessions were chosen over JWT for
exactly one reason (Arch §8), so a suite proving login works but never proving a session can be
killed has not tested the decision. C2's own wording is the acceptance criterion: *delete the
session row and assert the next request returns 401.*

Six decisions this task forced:

1. **⚠️ Revocation goes through `SessionStore(session_key=...).delete()`, never
   `Session.objects.filter(...).delete()`.** On the `cached_db` backend the row is the *slower* of
   two copies; a raw row delete leaves the cached copy live and the session keeps authenticating
   until the cache entry expires on its own. That is a silent security hole — the ORM call looks
   correct, the row really does vanish, and the revoked session still works. The test deletes via
   the store *and* asserts the next request is `401`, so a future "optimization" to a queryset
   delete fails loudly. `revoke_all_sessions()` therefore iterates unexpired sessions, decodes each
   one, and matches on `SESSION_KEY` rather than issuing one bulk `DELETE`.
2. **`start_session()` delegates to `django.contrib.auth.login()`.** It calls `cycle_key()`, which
   is the session-fixation defence; a hand-rolled `request.session[SESSION_KEY] = ...` would set
   the same three keys and *look* equivalent while leaving an attacker-planted pre-login token
   valid. `backend=` is passed explicitly because our user comes from a service function rather
   than `authenticate()`, and `login()` cannot infer it otherwise.
3. **⚠️ The password is checked BEFORE the account status.** Reporting `403 ACCOUNT_LOCKED` to
   someone who did not supply the password would confirm both that the account exists and that it
   has been suspended, to anyone who asked. A test asserts a wrong password on a suspended account
   reports invalid-credentials, not locked.
4. **`AccountLockedError` subclasses `AuthenticationError`** so `except AuthenticationError` still
   catches both — the fail-closed direction. A test asserts the subclass relation, because if it
   were ever broken a caller with only the broad `except` would fall through and treat a suspended
   account as authenticated. The view's two `except` clauses are ordered locked-first for the same
   reason; reversing them turns every locked account into a `401` and nothing fails loudly.
5. **`authenticate_user()` hashes a throwaway password on the unknown-identifier path.** Otherwise
   the unknown-user path returns without an Argon2 verification and is measurably faster than the
   wrong-password path, which turns response *timing* into the enumeration oracle that the
   identical error messages exist to prevent. Same mitigation as Django's own `ModelBackend`.
6. **`requires2fa` ships now, hardcoded `False`.** It is in the API §6.1 body, so a client written
   against the spec must not have to handle its absence, and `False` is truthful while no user can
   have a confirmed OTP device. ⚠️ When T1.7 lands this becomes a real check **and** `LoginView`
   must stop issuing a full session in the same breath — the spec puts `/auth/2fa/verify` on a
   *partial* post-password session, so returning `True` without that change would tell the client
   2FA is pending after already granting full access. The docstring carries that warning.

⚠️ **One defect fixed outside this task's surface: DRF was returning `403` where the spec says
`401`.** `APIView.handle_exception` rewrites `NotAuthenticated` to `403` when no authenticator
offers a `WWW-Authenticate` header, and `SessionAuthentication.authenticate_header()` returns
`None` by design (Django #20760, django-rest-framework #6021). API §4.2 fixes the distinction —
`401 UNAUTHENTICATED` means "show me a credential", `403 FORBIDDEN` means "I see who you are and
you may not" — and api-conventions.md states every protected endpoint implicitly returns `401`.
This was found by the logout-without-a-session test, and it was **not** a wrong test: left alone,
every protected endpoint in the project would have shipped the wrong status. Corrected once in
`urbenmend_exception_handler` rather than per-view, so T1.5 onward inherits it.

Related: `InvalidCredentials` is a plain `APIException`, deliberately **not** DRF's
`NotAuthenticated` or `AuthenticationFailed` — `handle_exception` special-cases exactly those two,
so either one would turn every bad-password reply into the `403` the spec forbids. `AccountLocked`
likewise cannot be DRF's `PermissionDenied`: that class's `default_code` is in `_DRF_DEFAULT_CODES`,
so the handler would flatten it to the generic `FORBIDDEN` and the client would lose the one signal
separating "retry" from "contact support".

Also settled here: `LoginSerializer` validates *shape only* — no `EmailField`, no `min_length` on
`password` — because rejecting a malformed identifier before the credential check tells an attacker
their guess was not even a valid address, and a length rule on login leaks the password policy;
`trim_whitespace=False` on the password, since a leading space is part of the secret. The login
response carries exactly `{id, role, preferredLanguage}`, with a test asserting the body contains
neither the email nor the phone — a login body is the easiest place for contact details to creep in
against API §2.1. `end_session()` performs no authorization check: a caller can only ever end the
session they presented, so "self" is structural rather than a rule to enforce.

Verified in the container: `ruff check` and `ruff format --check` clean, `mypy --strict` clean
(109 files), `pytest` **233 passed / 1 xfailed**, `makemigrations --check --dry-run` exit 0.

### T1.4 — CSRF protection for state-changing requests

✅ **Verified, no new code (2026-08-07).** The plan's own note — *"carried by DRF
`SessionAuthentication`"* — is accurate, and the configuration was already in place from A4/T0.3:
`CsrfViewMiddleware` in `MIDDLEWARE`, `SessionAuthentication` as the sole
`DEFAULT_AUTHENTICATION_CLASSES` entry, `CSRF_USE_SESSIONS = False` +
`CSRF_COOKIE_HTTPONLY = False` for the double-submit pattern API §2 specifies,
`CSRF_COOKIE_SAMESITE = "Lax"`, and `CSRF_COOKIE_SECURE` set per environment. **This task was
therefore proving the mechanism, not building it** — the 5 tests in
`identity/tests/test_csrf.py` are the deliverable.

⚠️ **Writing the test is not optional just because the framework supplies the behaviour.** "DRF
handles it" is an assumption until a test fails when it stops being true. The specific way it could
silently break: `SessionAuthentication` enforces CSRF *only* when it is the authenticator that
resolved the user, so any future view that sets `authentication_classes = []` — as `LoginView` and
`RegisterView` legitimately do — drops CSRF enforcement with no error and no warning. A test that
asserts the `403` is what catches that on the day someone copies the pattern onto a view that
carries a session.

Two things this task settled:

1. **CSRF enforcement is scoped to authenticated requests, and that is deliberate.** A first-time
   visitor has never been issued a token, so demanding one on `POST /auth/register` or
   `POST /auth/login` would make sign-up and sign-in impossible. Those endpoints set
   `authentication_classes = []`, which is what makes them exempt. The high-severity threat — an
   attacker submitting state-changing requests *as the victim* — requires the victim's session
   cookie, and every endpoint that carries one is protected. ⚠️ Login CSRF (tricking a victim into
   authenticating as the *attacker's* account) is a real but lower-severity residual risk that this
   scoping accepts; recorded here so it is a known decision rather than an oversight.
2. **The CSRF token rotates on login, separately from the session key.** `django.contrib.auth
   .login()` calls `rotate_token()` as well as `cycle_key()`. Because `CSRF_USE_SESSIONS = False`
   the token rides in its own cookie, so T1.3's session-key rotation does not cover it — a token
   planted before login would otherwise stay valid against the newly authenticated session. Tested
   as its own case for that reason.

Also worth noting for whoever writes the tests next: `test_logout_without_csrf_token_returns_403`
sets `client.handler.enforce_csrf_checks = True` **and** pops the CSRF cookie. Dropping the cookie
alone is not enough — Django's test client fakes a token by default, so the test would pass while
proving nothing. That combination is the pattern to copy for every future CSRF test.

`security.W016` (`CSRF_COOKIE_SECURE` not set) appears under `dev` settings only, by design —
`Secure` cookies cannot be sent over plain HTTP, which would break local development. `prod`
settings check clean.

Verified in the container: `pytest urbenmend/identity/tests/test_csrf.py` **5 passed**; full suite
**238 passed / 1 xfailed**; `ruff check`, `ruff format --check`, `mypy --strict` (109 files) clean;
`makemigrations --check --dry-run` exit 0; `check --deploy` clean on `prod`.

### T0.10 — category taxonomy (❓Q1 resolved)

**❓Q1 is closed (2026-08-07): the PRD §6.2 seven-node draft is confirmed as-is.** Recorded in
PRD §6.2/§15 and ops §4; this unblocks T1.5's BR-26 scoping and T1.6's authority provisioning.

Built `urbenmend/classification/models.py` (`Category`, `CategoryStatus`), migration `0001_initial`
seeding the seven nodes, `CategoryAdmin`, and 7 tests.

Decisions that bind later work:

- **Flat, not hierarchical** — §6.2 lists seven peers and data-model §5 gives Category no parent.
  Nesting later is an additive migration; un-nesting after reports accumulate is not.
- ⚠️ **`Other / Uncategorized` is a required fallback sink, not filler.** PRD §331: LLM triage
  returning an out-of-set category coerces to `Other`. Retiring or deleting it breaks FR-13a. A test
  asserts it exists and is `active`.
- **`slug` is the machine key, labels are display-only** — classification, the LLM adapter, and
  authority-scope rows all reference `slug`, so renaming an English label cannot break them.
  ⚠️ **Corrected in T1.5:** `0001` shipped without the column and `name_en` was doing the job.
  `classification/0002` adds and backfills it — see the T1.5 record below.
- **`name_bn` is non-null and asserted not to equal `name_en`** (NFR-8). A placeholder-Bangla seed
  passes a not-null check and still ships an untranslated UI to every Bangla-speaking user.
- **Taxonomy is data, not code** (NFR-11/FR-30): a seeded table, not a Python enum. `CategoryAdmin`
  disables add and delete — additions come from reviewed migrations, and `Active → Retired`
  (data-model §5) preserves historical Report references.
- ⚠️ **The seed's `RunPython` reverse deletes only the seven seeded slugs**, never
  `Category.objects.all()` — a reverse that truncates the table would destroy operator-added rows.
  Verified by a real `migrate classification zero` → `migrate` cycle, per `database.md`.

Verified in the container: `pytest urbenmend/classification/` **7 passed**; full suite **245 passed /
1 xfailed**; `ruff check`, `ruff format --check`, `mypy --strict` (111 files) clean;
`makemigrations --check --dry-run` exit 0; migration reversibility confirmed both directions.

### T1.5 — RBAC enforcement layer (FR-3, BR-26/27)

Built the Authority↔Category scope relation and the service-layer primitives every later
authorization check calls: `has_role` / `require_role`, `has_category_scope` /
`require_category_scope` / `require_scoped_visibility`, `scoped_category_ids`, plus
`AuthorizationError` and the `category_scope_for` selector. Two migrations —
`classification/0002_category_slug`, `identity/0003_category_scope` — and 26 tests.

⚠️ **Found and fixed a T0.10 gap first.** The T0.10 record above claimed `slug` was the machine
key, but `0001` never created the column — `name_en` was the only identifier. API §6.2 emits scope
as `"categoryScope": ["roads","water_drainage"]` and §6.10 addresses nodes as
`PATCH /categories/{key}`; neither is an English label, and the spec is authoritative. `0002` adds
the column **nullable → backfill → tighten to NOT NULL UNIQUE**, the backward-compatible shape a
single `AddField(unique=True)` cannot have against seven populated rows. `roads`,
`water_drainage` and `electrical` are quoted verbatim in the spec, so those three are contract;
the other four follow the same convention.

Decisions that bind later work:

- ⚠️ **An empty scope grants nothing.** `has_category_scope()` asks whether the category is
  present, so a freshly provisioned Authority can act on nothing until an Admin scopes them
  (BR-25). Reading empty as "unrestricted" would turn a forgotten provisioning step into access to
  every category — the exact failure BR-26 exists to prevent.
- ⚠️ **`has_role()` checks `status`, not just `role`.** A suspended Authority still reads
  `role == "authority"`; trusting the column alone would let it keep acting until its session
  expired, which is the failure sessions-over-JWT was chosen to prevent (Arch §8). A test asserts
  the role column is unchanged while the check denies.
- ⚠️ **Admins bypass scope; they are not scoped to everything.** `scoped_category_ids()` returns
  `None` for Admin, meaning "apply no filter" — seeding a row per category would silently un-scope
  an Admin the moment a migration adds a node.
- ⚠️ **`403` to act, `404` to see.** `require_category_scope()` raises `403`;
  `require_scoped_visibility()` raises `404`. API §4.2 defines `404` as "absent **or hidden from
  this caller**", and a `403` on a scoped read confirms the id resolves to a real Issue in another
  category — enough to enumerate ids and map another department's workload. Two functions rather
  than a flag, so the call site states which it means.
- ⚠️ **The scope check is `.filter(pk=...).exists()`, never `category in user.category_scope.all()`.**
  The latter caches prefetched rows on the instance, so a scope an Admin just revoked keeps
  passing for the life of the object — the same stale-authorization hazard `revoke_all_sessions()`
  addresses for sessions. A test revokes mid-test and asserts the next check denies.
- **Scope rows are allowed on any role and are inert off Authority.** No DB constraint ties them
  to the role, because BR-25 promotes a Citizen to Authority and a constraint would have to be
  dropped to allow it. The role check runs first, so stray rows on a Citizen grant nothing.
- **`AuthorizationError` subclasses Django's `PermissionDenied`, not DRF's.** `services.py` stays
  free of DRF imports (Arch §3.1) so a service is callable from a Celery task;
  `urbenmend_exception_handler` already maps it to the `403 FORBIDDEN` envelope. A test pins that
  mapping, since nothing else can.
- **Denial messages name neither role nor resource.** "Authority role required", repeated across
  endpoints, maps the §4.2 permission matrix from the outside. A test asserts the message is
  silent on both.
- ⚠️ **`classification/0002`'s reverse is `RunPython.noop`, deliberately.** The first version
  cleared the column (`update(slug="")`) and **failed the down migration** — seven rows sharing
  `""` violate the UNIQUE index. There is no state to restore: the `AddField` reversal drops the
  column moments later. Caught by running the cycle, not by reading it, which is why
  `database.md` gates both directions.

Verified in the container: `pytest urbenmend/identity/tests/test_rbac.py urbenmend/classification/`
**33 passed**; full suite **271 passed / 1 xfailed**; `ruff check`, `ruff format --check` (119
files), `mypy --strict` (112 files) clean; `makemigrations --check --dry-run` exit 0;
`check --deploy` clean on `prod`; reversibility confirmed by a real
`migrate identity 0002` → `migrate classification zero` → `migrate` cycle, with all seven rows and
their slugs intact afterwards.

### T1.6 — Admin provisions authority accounts + category scope (FR-2, BR-25)

`POST /users/authorities`. Two services — `provision_authority()` and `set_category_scope()` — plus
the shared `_resolve_category_scope()` slug resolver, `ProvisioningError`, a
`ProvisionAuthoritySerializer`, `ProvisionAuthorityView`, `UserSerializer.categoryScope`,
`identity/0004_authority_two_factor`, admin scope editing, and 38 tests.

⚠️ **BR-25's audit obligation is only partly met, on purpose.** The rule is "the grant is audited"
(FR-32), but the immutable audit log is **T8.1 in P8**, where the append-only property is enforced
*at the database level* by revoking `UPDATE`/`DELETE` from the application role — "application
discipline alone will not satisfy NFR-10". Building the table now would either fix its schema seven
phases before its design exists, or ship it without the revoke and manufacture the false assurance
NFR-10 exists to prevent. So every privileged action routes through one funnel,
`_audit_privileged_action()`, which currently writes a structured log line. **A log line is not an
audit record** — it is mutable, expires with retention, and is not queryable via
`GET /audit-events`. T8.1 replaces that function's body; it must not add a second call path beside
it, or the swap will miss callers. M1's DoD does not mention audit and M8's does, so the plan agrees
with the sequencing.

Decisions that bind later work:

- ⚠️ **The provisioned account has an unusable password, so it cannot log in until T1.7.** The
  spec's body carries no password field. The alternatives are an Admin choosing another person's
  credential, or a generated secret travelling back in the API response — both worse than an
  account awaiting its own reset flow. Role and scope are fully real; only the credential path is
  missing.
- ⚠️ **`status` is `registered`, not `active`.** The work address is unproven until someone reading
  that mailbox verifies it, and BR-30 bars notifications to an unverified channel. An Admin typo
  would otherwise create a live Authority whose owner never learns the account exists.
- ⚠️ **Retired categories are rejected (`422`), not silently dropped.** A Retired node can never
  match an Issue, so scoping to one grants nothing while reading back as a successful grant — the
  provisioning bug hardest to notice, because the account looks correctly configured.
- ⚠️ **`409` here is specific where registration's is generic.** Registration is public and must
  not confirm an address is taken; this endpoint is Admin-only, and an Admin who cannot be told
  "that address already has an account" cannot do the job. A test asserts the two differ.
- ⚠️ **The duplicate check normalizes with `.lower()`, not `BaseUserManager.normalize_email`**,
  which lowercases only the domain. `Admin@x.com` would pass an unnormalized check, then store
  lowercased, and surface the collision as a `500 IntegrityError` instead of the documented `409`.
  The `IntegrityError` catch stays anyway — it closes the window between the check and the INSERT
  when two Admins provision the same address concurrently.
- ⚠️ **`set_category_scope()` replaces, never merges.** The spec sends the whole array, so a merge
  would make revocation impossible through the documented body — an Admin narrowing a scope would
  silently widen it. An empty array is a valid request that revokes everything: the way to park an
  account without suspending it.
- **The target of a scope change is checked against the `role` column, not `has_role()`.** Scope
  must stay editable on a *suspended* Authority, or an Admin could not correct a scope before
  reinstating them.
- ⚠️ **No `IsAdminUser` on the view.** DRF's checks `is_staff` — Django-admin plumbing, not the
  domain `role` column. `require_role(actor, Role.ADMIN)` inside the service is the enforcement
  point (FR-3); a permission class here would read as one and drift from it.
- **`role` and `status` in the request body are ignored, not honoured.** The serializer has no such
  fields. `POST /users/authorities` says what it makes, and an Admin minting another Admin through
  it would be an undocumented privilege path. Two tests assert the escalation attempts fail.
- **`require_two_factor` is a stored column now, enforced in T1.7.** API §6.2 sends
  `requireTwoFactor`; discarding a documented input would tell the Admin the account requires 2FA
  while nothing recorded that it does.
- ⚠️ **Admin's scope editor bypasses `set_category_scope()` and therefore the audit funnel.** It is
  the break-glass path for FR-30/31; the API path is the audited one. T8.1 should reconsider whether
  admin may write that M2M at all.
- ⚠️ **`users/authorities` must stay routed before any `users/<id>` pattern.** A `<uuid:pk>` would
  not shadow it, but T1.9's looser `<str:pk>` would — turning provisioning into a lookup for a user
  whose id is the literal string `"authorities"`.
- ✅ **`UserSerializer.categoryScope` reads `[]` for an Admin**, which is the stored truth but not
  the effective permission — Admins bypass scope. API §6.2 only showed an `"role":"authority"` body
  and said nothing about the other two roles. **Resolved in T1.9 (2026-08-07): §6.2 was amended
  before `GET /users/me` shipped**, per the spec-first rule — one response shape for all three roles,
  plus a table for what `[]` means per role. Not invented here, and T1.6 was unaffected: this
  endpoint only ever returns an Authority.

Verified in the container: `pytest urbenmend/identity/tests/test_provisioning.py` **38 passed**;
full suite **309 passed / 1 xfailed**; `ruff check`, `ruff format --check` (120 files),
`mypy --strict` (113 files) clean; `manage.py check` clean; `makemigrations --check --dry-run`
exit 0; `check --deploy` clean on `prod`; `0004` reversibility confirmed by a real
`migrate identity 0003` → `migrate identity` cycle, asserting the column dropped and returned.

### T1.7 — Two-factor authentication for authority/admin (FR-4)

`POST /auth/2fa/enroll` + `POST /auth/2fa/verify`, on TOTP via `django-otp`. Adds
`requires_two_factor()` / `start_partial_session()` / `resolve_partial_session_user()` /
`enroll_totp_device()` / `verify_totp()` and the `TwoFactorError` / `TwoFactorEnrollmentError`
exceptions to `identity/services.py`; three serializers; `TwoFactorEnrollView` /
`TwoFactorVerifyView` and the shared `_resolve_caller()`; 29 tests. **No migration** — `TOTPDevice`
is django-otp's model and its three migrations were already applied.

**⚠️ The API spec was amended first, as the rules require.** §6.1 specified `/auth/2fa/verify` with
no way to obtain a device, and did not list the absence under its own "Missing endpoints —
considered and resolved". That made `requireTwoFactor: true` (stored by T1.6) a permanent lockout
and `/auth/2fa/verify` unreachable code. `POST /auth/2fa/enroll` was added, `/auth/2fa/verify` was
documented as also confirming an enrolment, and the amendment is recorded in §9 with its reasoning.

- ⚠️ **The partial session works by NOT calling `django_login()`.** It writes one non-standard key
  into an anonymous session, so `SESSION_KEY` (`_auth_user_id`) stays unset, `request.user` resolves
  to `AnonymousUser`, and every authenticated endpoint rejects the cookie with `401` automatically.
  **django-otp's `otp_required` decorator model was considered and rejected**: it logs the user in
  fully and then gates views individually, so a view added later without the decorator is reachable
  with one factor. That fails open; this fails closed with no per-view gate to forget.
- ⚠️ **`LoginView` stopped issuing a full session in the same change that made `requires2fa` real** —
  the trap T1.3 recorded. `LoginView` and `LoginResponseSerializer` both call
  `services.requires_two_factor()`, so the cookie and the body cannot disagree.
- ⚠️ **The `user` object is omitted from the login body while `requires2fa` is true.** The role is
  the fact worth withholding from a password-only holder: it tells them whether the account they are
  part-way into is an Authority or an Admin. `null` would leak the same distinction by shape.
- ⚠️ **`start_partial_session()` calls `cycle_key()` explicitly.** `start_session()` gets rotation
  free from `django_login()`; this path does not, and without it a planted pre-login token would be
  the one carrying the partial credential — session fixation, one factor earlier than usual.
- ⚠️ **`resolve_partial_session_user()` re-fetches the user and re-checks `is_active`.** The password
  step happened on an earlier request; BR-25 suspension is meaningless if a login already in flight
  completes anyway. A test suspends an account between the two calls and expects `403`.
- ⚠️ **`device.key` is hex; `config_url` is base32 — publishing the wrong one is invisible.** The
  first draft returned `device.key` as `secret`, giving a response whose `secret` and `otpauthUri`
  disagree: the QR code works and manual entry silently produces wrong codes forever. The service now
  returns `(device, secret, otpauth_uri)` with both derived from `bin_key`, and a test asserts
  `secret=` appears inside the URI. Caught by the tests, not by review.
- ⚠️ **`verify_token()` does the checking and must not be reimplemented.** It stores `last_t`, which
  is what stops a code being replayed inside its own 30-second window — precisely the window an
  attacker who intercepted one code needs. A hand-rolled comparison looks equivalent and has no
  replay protection at all.
- ⚠️ **A confirmed device is preferred over an unconfirmed one in `verify_totp()`.** Checking the
  unconfirmed one first would let someone holding a live session enrol their own device and
  authenticate against it, sidestepping the `409`.
- ⚠️ **`requires_two_factor()` is true for a flagged account with no device — deliberately.** That
  combination is a login the user cannot complete, and it must stay that way; reading it as "2FA not
  required" would let an Admin believe an account is protected while a password alone opens it.
  `POST /auth/2fa/enroll` accepts a **partial** session precisely so such an account can enrol out of
  the lockout. An *unconfirmed* device does not opt an account in — an abandoned setup must not lock
  anyone out.
- **`enroll_totp_device()` has no `require_role()` check.** FR-4 targets authorities and admins by
  policy, but a citizen protecting their own account is not privilege escalation. Authorization is
  "self", enforced structurally — the caller can only pass their own user.
- **Unconfirmed devices are replaced on re-enrolment; a confirmed one is never silently replaced.**
  Overwriting a confirmed device would let anyone with a live session swap the second factor.
- **One generic message for every verify failure** (`_GENERIC_TWO_FACTOR_FAILURE`) — "no device
  enrolled" would tell a password-only caller whether the account has 2FA at all.
- ⚠️ **Both 2FA routes are the only ones in the project accepting a non-authenticated session.**
  Anything else added under `auth/2fa/` needs that decision made deliberately, not inherited.
- **`/auth/password/forgot`·`/reset` is still unbuilt and now explicitly unowned.** `api/urls.py`'s
  comment previously routed it to T1.7; the plan's T1.7 row is 2FA-only and reset traces to FR-1
  (T1.2's row), where it was never built. Delivery is blocked on ❓Q5 the same way T1.2's verification
  codes are. The comment was corrected rather than the scope silently widened.

Verified in the container: `pytest urbenmend/identity/tests/test_two_factor.py` **29 passed**; full
suite **338 passed / 1 xfailed**; `ruff check`, `ruff format --check` (121 files), `mypy --strict`
(114 files) clean; `manage.py check` clean; `makemigrations --check --dry-run` exit 0;
`check --deploy` clean on `prod`. No migration to reverse — `showmigrations otp_totp` confirms all
three third-party migrations already applied.

### T1.8 — Login/OTP rate limiting + account lockout (FR-4, API §4.5)

Built `urbenmend/api/throttling.py` (`ScopedWindowRateThrottle`, `AuthAnonRateThrottle`,
`AuthIdentityRateThrottle`, `AuthUserRateThrottle`, `RateLimitHeadersMixin`,
`clear_identity_throttle`), `AUTH_THROTTLE_RATES` in `settings/base.py`, throttle wiring on the five
auth views, the root `conftest.py`, and 21 tests in `identity/tests/test_rate_limiting.py`.
**No migration** — the mechanism is entirely Redis-backed. Full suite **359 passed / 1 xfailed**,
mypy (116 files) / ruff clean, no drift, `check --deploy` clean on `prod`.

- ⚠️ **Lockout is throttle-only backoff — a scoping decision, not an oversight.** FR-4 says
  "lockout/backoff" and API §6.1 lists `403 ACCOUNT_LOCKED`, but neither doc says what brute-force
  protection *does*. Persistent per-account lockout state was rejected because it is a targeted DoS:
  anyone who knows an Authority's email could hold them out on demand. Failed logins consume a
  bucket; success clears it. `403 ACCOUNT_LOCKED` stays what T1.3 made it — a `status` denial.
  `test_a_locked_out_identifier_does_not_lock_the_account_itself` pins the difference.
- ⚠️ **The numbers are our policy, not spec-derived.** `api-conventions.md` lists "numeric rate
  limits and windows" under **Not specified — do not invent**, but FR-4 and the M1 gate require the
  endpoints to be limited. Same tension T1.2 resolved for `CODE_LENGTH`/`TTL`/`MAX_ATTEMPTS`, same
  resolution: `10/15m` per IP, `5/15m` per identifier, `20/15m` per session — in settings,
  env-overridable (NFR-11), labelled as chosen. They become contract only if the spec adopts them.
- ⚠️ **`SimpleRateThrottle.parse_rate` reads only `period[0]`, so DRF cannot express a 15-minute
  window.** `"5/15m"` there means 5-per-*minute*: 15× tighter than written, with nothing anywhere to
  indicate it. Verified against the installed source, not assumed. `ScopedWindowRateThrottle`
  overrides it; `test_parse_rate_honours_a_multi_unit_window` is the regression guard, and a bare
  `"10/hour"` still parses exactly as DRF does.
- ⚠️ **DRF emits none of the three `RateLimit-*` headers API §4.5 requires on every limited
  endpoint** — only `Retry-After`, only on a 429. `check_throttles()` also keeps its throttle
  instances local and stores *nothing* on the request, so there is no post-hoc state to read: the
  first attempt read invented `request.throttle_wait`/`throttle_duration` and would have emitted no
  headers at all, silently. `RateLimitHeadersMixin` overrides `get_throttles()` to capture the
  instances DRF actually uses. Same family as the T0.6 camelCase and pagination gaps.
- ⚠️ **The advertised bucket is the one with least *headroom*, not the smallest limit.** A
  wide-but-nearly-spent bucket constrains the caller more than a narrow fresh one; advertising the
  wrong one tells a well-behaved client it has room it does not have.
- ⚠️ **`AuthIdentityRateThrottle` keys on the submitted `identifier`, not `request.user`.** At login
  there is no user yet — keying on `request.user` would only throttle *after* a successful password
  check, i.e. never during the attack it exists to stop.
- ⚠️ **The identifier is SHA-256'd into the cache key, never stored raw.** Keys surface in
  `redis-cli KEYS`, slow logs and dumps; an email there is PII (NFR-12) and a phone number is worse.
  Normalized first, or casing alone multiplies the allowance.
- ⚠️ **`get_cache_key` swallows `request.data` parse errors and returns `None`.** An exception there
  aborts `check_throttles()` mid-list, so the per-IP bucket registered *after* it would never count
  the request — a flood of malformed bodies would consume no budget at all.
- ⚠️ **`clear_identity_throttle()` is success-path only, and clears the identifier bucket alone.**
  Moving it earlier (or into a `finally`) erases the counter that makes the endpoint limited, and
  every pre-existing test still passes because the happy path never observes it. The per-IP bucket
  deliberately survives a success: one source working a credential dump lands a valid login every so
  often, and clearing on those would hand it an unlimited overall budget.
- ⚠️ **The throttle runs before the credential check**, so a correct password submitted while
  throttled is still refused — the run that finds the password is exactly the one that must not pass.
- ⚠️ **`AuthAnonRateThrottle` throttles authenticated callers too**, unlike DRF's `AnonRateThrottle`,
  which returns `None` the moment a user is present. On auth endpoints the per-IP cap exists to stop
  one source spraying *many* identifiers; exempting session-holders would leave that unlimited.
- ⚠️ **A partial post-password session is unauthenticated by T1.7's design, so 2FA code-guessing
  lands in `auth_anon`, not `auth_user`.** That is the bucket to tighten if OTP ever needs to be
  stricter — narrowing `auth_user` would only constrain callers who already passed both factors.
  Unlike `verify_code()` (T1.2), `verify_totp()` has no per-account attempt counter: `verify_token()`
  blocks replay of a code, not a walk through the keyspace, so these buckets are the only cap.
- ⚠️ **Rates are read at throttle *instantiation*, not bound as a class attribute.** DRF's
  `THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES` binds once at import, so `override_settings`
  would not reach it — and a limit that cannot be turned down in a test is a limit whose 429 path
  never gets exercised.
- ⚠️ **`DEFAULT_THROTTLE_CLASSES` is deliberately left unset.** A project-wide default would silently
  throttle the public map and issue list, which §4.5 does not ask for and Q7 makes unauthenticated.
- ⚠️ **The root `conftest.py` exists for one reason: throttle state is not rolled back.**
  `pytest-django` wraps each test in a transaction, but counters live in Redis. Adding the throttles
  turned **27 pre-existing tests red** with spurious 429s in an order-dependent way. The fix is an
  autouse session-wide cache clear — not looser limits, and not one test rewritten to accommodate
  the throttle. Safe because sessions are `cached_db` (the DB row is the source of truth); it would
  not be under a pure `cache` session backend.
- **No spec amendment was owed.** §4.5 and the `429`s in §6.1 already describe exactly this; the only
  unspecified part is the numbers, which the docs leave open on purpose.

Verified in the container: `pytest urbenmend/identity/tests/test_rate_limiting.py` **21 passed**;
full suite **359 passed / 1 xfailed** (up from 338); `ruff check`, `ruff format --check` (124 files),
`mypy` (116 files) clean; `manage.py check` clean (0 silenced); `makemigrations --check --dry-run`
exit 0; `check --deploy` clean on `prod` with a realistic key.

### T1.9 — Profile read/update + account deletion → PII anonymization (P6, BR-33, C-14)

Built `update_profile` / `anonymize_account` and `ProfileUpdateError` / `AccountDeletionError` in
`identity/services.py`, `ProfileUpdateSerializer`, `MeView` (GET/PATCH/DELETE), the `users/me` route,
and 36 tests in `identity/tests/test_profile.py`. **No migration** — every column this touches
already exists, including the `status=deleted` escape hatch A6 put in the CheckConstraint for exactly
this endpoint. Full suite **395 passed / 1 xfailed**, mypy (117 files) / ruff clean, no drift,
`check --deploy` clean on `prod`.

- ⚠️ **The spec was amended first, three times, before any of this was written.** §6.2 documented
  `GET /users/me` with an Authority-shaped example only, said nothing about what `categoryScope`
  means for the other two roles, and gave `DELETE /users/me` no role restriction at all. All three
  are now written down: one response shape for every role, a table for what `[]` means per role, and
  Citizen-only deletion. The T1.6 record flagged the first of these as **"amend the spec before
  T1.9's `GET /users/me`"** — that ❓ is now closed.
- ⚠️ **An Admin's `categoryScope: []` and an unscoped Authority's `categoryScope: []` are
  byte-identical JSON meaning opposite things** — unrestricted versus permitted-nothing. `role` is
  the only disambiguator, and the spec now says so and warns clients not to derive capability from
  the field alone. It is emitted for every role anyway, because the alternative (omit, or `null`)
  makes a client branch on `role` before it can parse the body.
- ⚠️ **`DELETE /users/me` is Citizen-only, and that came from the data-model, not from §6.2.** The
  "Ownership & Permissions" matrix grants an Authority `RU` on its own account — not `D`. Authority
  accounts are admin-provisioned (FR-2) with audited grants (BR-25), so the holder erasing one would
  destroy an audited record; an Admin self-deleting could strand the platform with no account able to
  provision anyone. Both get `403`, and the spec now names the alternative path
  (`PATCH /users/{id} {"status":"deprovisioned"}`).
- ⚠️ **Anonymization nulls the PII in the *same* `save()` as the status flip.** Two UPDATEs would
  leave a window where a crash produces a row that is `deleted` but still carries a live email — and
  the constraint's DELETED branch would happily accept it. Anonymization that can silently fail
  halfway is not anonymization.
- ⚠️ **The row is retained, never deleted** (C-14): public Issue history keeps a stable author
  reference. `test_anonymization_retains_the_row` is the test that fails if someone "simplifies" this
  to `user.delete()` — every PII assertion in the file would still pass.
- ⚠️ **The `verificationcode` and `TOTPDevice` deletes are explicit because no cascade fires.** Both
  FKs are `CASCADE`, but nothing is deleted here — the user row survives — so leaving them implicit
  would retain a live TOTP secret on an anonymized account.
- ⚠️ **Sessions are revoked inside the transaction, and `is_active` is not a substitute.** `status =
  DELETED` makes the derived `is_active` False, which stops *new* authentications at commit, but a
  live session keeps working until it expires (Arch §8, T1.3). BR-33 wants both.
- ⚠️ **`202` is returned to a caller that can no longer authenticate.** The status code reflects that
  retained-record anonymization may extend past the response (P2/P3 add Reports and media) — not that
  the account is still usable. The spec now says this explicitly, because `202` reads as "queued" and
  a client could reasonably infer the session survives it.
- ⚠️ **`PATCH /users/me` rejects unknown fields rather than dropping them.** DRF's default is silent
  omission, so `PATCH {"role":"admin"}` would answer `200` with a body still reading
  `role: "citizen"` — indistinguishable from success. `api-conventions.md` asks for rejection "where
  strictness matters"; an endpoint whose neighbouring columns are `role`, `status` and `is_staff` is
  that case. Both the snake_case and camelCase spellings are allowed, derived from the declared
  fields, because the camelCase mixin rewrites keys *before* `validate()` runs while `initial_data`
  keeps the client's original.
- ⚠️ **`ProfileUpdateSerializer` is a plain `Serializer`, not a `ModelSerializer` over `User`.** A
  model serializer's field set is whatever the model carries, so `role`/`status`/`is_staff`/
  `require_two_factor` would each be one `fields` edit — or one careless `"__all__"` — from being
  self-assignable. Escalation should require *adding* a field, not forgetting to exclude one.
- ⚠️ **`email` is absent from the update body deliberately.** It is the address a password reset is
  sent to, so a self-service change converts one borrowed session into permanent account takeover.
  `update_profile` has no `email` parameter at all, and a test asserts the signature rather than the
  behaviour — the guarantee is structural.
- ⚠️ **Any submitted `phone` clears `phone_verified_at`, even when the value is unchanged.** The
  timestamp is a claim that *this* number was proven; skipping the clear when the value matches keeps
  a stale claim alive for a number that changed hands and came back. Fail closed (BR-30).
- ⚠️ **`""` clears the phone; `null` is refused.** Accepting both spellings for one intent means a
  client sends `null` and hits the E.164 validator as a `500` instead of a `400`. `save()` then
  normalizes `""` to the NULL the UNIQUE index needs — Postgres allows many NULLs under UNIQUE but
  only one `""` (A6).
- ⚠️ **Clearing the last contact channel is `422`, not `400`.** An account with neither email nor
  phone is unreachable and unrecoverable (data-model §1) — a business-rule rejection, not a malformed
  body.
- ⚠️ **The `409` check excludes the caller's own row.** Without `.exclude(pk=user.pk)`, re-submitting
  an unchanged profile would conflict against itself. The `IntegrityError` catch stays regardless: it
  closes the window between the check and the UPDATE, the same race `provision_authority` records.
- **`403` here is raised as Django's `PermissionDenied`, not `AccountLocked`.** §6.2 names no
  endpoint-specific code for this, so it renders as the generic `FORBIDDEN` from `_STATUS_TO_CODE`;
  inventing one would put a contract decision in a view. Contrast `ACCOUNT_LOCKED`, which §6.1 names.
- **`MeView` carries `auth_user` only, not the per-IP bucket.** `auth_anon` is sized for pre-session
  auth attempts; applying it to a profile endpoint would let one user's edits exhaust the login
  allowance for everyone behind the same NAT.
- **`GET /users` and `PATCH /users/{id}` are out of scope and now explicitly unowned.** Both are
  Admin endpoints in §6.2 that `api/urls.py` previously pointed at T1.9 in a comment. They need a
  task ID rather than silent absence — the same treatment `/auth/password/forgot`·`/reset` got.

Verified in the container: `pytest urbenmend/identity/tests/test_profile.py` **36 passed**; full
suite **395 passed / 1 xfailed** (up from 359); `ruff check`, `ruff format --check` (125 files),
`mypy` (117 files) clean; `makemigrations --check --dry-run` exit 0; `check --deploy` clean on `prod`
with a realistic key.

**M1 gate:** a citizen can register → verify → log in; an admin can provision a scope-limited
authority; RBAC denies out-of-scope actions; sessions revoke immediately.

- [x] register → verify (T1.2)
- [x] log in (T1.3 — sessions)
- [x] 2FA for authority/admin (T1.7 — TOTP; partial post-password session)
- [x] login/OTP rate limiting + backoff (T1.8 — ⚠️ throttle-only, no per-account lock state)
- [x] CSRF on state-changing requests (T1.4)
- [x] category taxonomy seeded (T0.10 — ❓Q1 resolved)
- [x] admin provisions a scope-limited authority (T1.6 — ⚠️ audited to a log line only until T8.1)
- [x] RBAC denies out-of-scope actions (T1.5)
- [x] sessions revoke immediately (T1.3)
- [x] profile read/update + deletion→anonymization (T1.9 — ⚠️ `/users/me` only; ⚠️ deletion is
      Citizen-only)

### T2.1 — Report entity + validation (P2, FR-5/6, BR-2/3/35, C-3/C-11)

Built the `Report` model and the `geo` app that makes its location checkable: `geo/models.py`
(`CityBoundary`), `geo/selectors.py` (`active_city_boundary`, `is_within_city`, `BoundaryUnavailable`),
`reporting/models.py` (`Report`, `ReportStatus`, `SeveritySignal`, `SEVERITY_RANK`,
`ClassificationSource`), `reporting/services.py` (`create_report`, `validate_location`,
`validate_report_content`, `ReportValidationError`), `OutOfCity` in `api/exceptions.py`, two admins,
migrations `geo/0001_initial` + `geo/0002_seed_city_boundary` + `reporting/0001_initial`,
`docs/city-boundary/dhaka-demo.geojson`, four `factory_boy` factory modules, and 59 tests across
`geo/tests/test_city_boundary.py`, `reporting/tests/test_models.py`, `reporting/tests/test_services.py`.
**No endpoint** — `POST /reports` is T2.2; this task ships the enforcement point it will call.
Full suite **454 passed / 1 xfailed** (up from 395), mypy (124 files) / ruff (132 files) clean, no
drift, `check --deploy` clean on `prod`, both new apps verified reversible in both directions.

- ⚠️ **Intake fails closed when no boundary is configured, declining the degradation Arch §409
  offers.** §409 sanctions skipping a check whose dependency is missing; C-11 says an out-of-city
  location "is not accepted". A silently-disabled containment check leaves no trace and is
  discovered months later in the data, so an empty boundary table rejects *everything* loudly
  instead. `active_city_boundary()` raises rather than returning `None`, precisely so no caller can
  write `if boundary and not contains(...)` — which accepts every location on Earth the moment the
  table empties. `test_intake_fails_closed_when_no_boundary_is_configured` is the assertion.
- ⚠️ **`GistIndex`, never `models.Index`, on `location`.** `models.Index` emits a B-tree, which
  cannot serve `ST_Within`/`ST_DWithin` — every spatial query silently degrades to a sequential scan
  while the migration reads as indexed. Paired with `spatial_index=False` on the field so Django does
  not also create a duplicate auto-named index. BR-35 containment, T4.4 clustering and the T7.4 map
  bbox all depend on this one line.
- ⚠️ **`422 OUT_OF_CITY` is its own exception type, not a `ReportValidationError`.** A well-formed
  coordinate outside the city is a business-rule violation, not a malformed body; collapsing it into
  the generic `400 VALIDATION_FAILED` leaves a client unable to distinguish "fix your JSON" from "we
  do not serve your city". Two tests pin the pair — one on the code, one on the types differing.
- ⚠️ **Location is validated before content, and the order is observable.** A submission that is both
  out-of-city and under-described returns the `422`: reporting the description first sends a citizen
  off to write more prose about a place UrbanMend does not serve, and they get the `422` on retry
  anyway. Authorization precedes both, so a non-Citizen learns nothing about which rules they broke.
- ⚠️ **The city boundary is seeded data, not code** (NFR-11, and the user's decision when asked).
  `geo/0002` loads `docs/city-boundary/dhaka-demo.geojson` through `apps.get_model()` and supplies a
  **real reverse callable** that deletes only the row named `Dhaka (development stand-in)` — never
  `.objects.all()`. Verified by a genuine down/up cycle, not by inspection: the first attempt reported
  "No migrations to apply" because they had never been applied, which is a green output proving
  nothing. GEOS is imported *inside* the function so `makemigrations --check` runs on a machine
  without the library.
- ⚠️ **Replacing the boundary is add-and-retire, never edit-in-place**, and `CityBoundaryAdmin`
  enforces it: `area` is read-only on change but writable on add, delete is denied outright. Exactly
  one row must be active — `active_city_boundary()` raises on zero *and* on two, so a half-finished
  swap surfaces as an error rather than validating reports against whichever row sorted first.
- ⚠️ **`ReportStatus` holds no Issue-workflow value, and a test asserts it by name.**
  `Acknowledged`/`In Progress`/`Resolved`/`Closed` are Issue statuses (PRD §6.3); adding one here
  would give two rows one answer to "is this fixed?" and let them disagree. It is a one-line,
  entirely plausible edit, which is why the assertion names the forbidden values rather than counting.
  `Draft` is omitted too — FR-8 marks it SHOULD and offline drafts are a PWA concern.
- ⚠️ **`SeveritySignal` is declared in `reporting` and shared with `issues`, and has four bands.**
  `03-data-model.md` §3 and BR-8 still show a stale three. Two independently declared enums would let
  one gain a band the other lacks, and BR-11's `max()` across them would then be undefined.
  `SEVERITY_RANK` exists **only** so "highest" is computable — it is not a score (FR-21 removed
  those), not tunable, not exposed, and must never be summed or weighted against corroboration or
  proximity, both display-only (C-10).
- ⚠️ **`is_classified` keys on `classified_at`, not on `category`.** A citizen's category *hint*
  populates `category` at intake (API §6.3), so keying on it would report an unclassified report as
  classified and T3.5's worker would skip it forever — with nothing logging an error.
- ⚠️ **A category hint is recorded *with its source*, and is not a classification.**
  `classification_source = citizen` (FR-11) attributes the choice instead of silently absorbing it,
  and `classified_at` stays null so the worker still picks the report up. An unknown *or retired* slug
  is refused, never coerced to `Other` — that coercion (BR-7) is for LLM output, where a value
  outside the taxonomy means an unusable answer; a human picking a retired node is running a stale
  client, and filing their report under `Other` would lose information they could have corrected.
  (T2.2 correction: the refusal renders as **`400 VALIDATION_FAILED`**, not the `422` this bullet
  originally claimed. §6.3 lists the taxonomy check among its validation rules and reserves `422` for
  `OUT_OF_CITY`; a value outside a controlled set matches `preferredLanguage: "fr"` on `/users/me`,
  which is `400`. The service was always right — only this sentence was wrong.)
- ⚠️ **`# noqa: DJ001` on `severity_signal` and `classification_source` is deliberate, against
  ruff's default advice.** DJ001 warns that a nullable string column has two spellings for empty —
  correct in general, wrong here: `""` is not a member of `SeveritySignal`, so it would be an
  undeclared fifth band that only validates because Django skips blanks, and `SEVERITY_RANK[""]`
  raises `KeyError` inside T4.6's `max()` at runtime. `confidence` is a `FloatField` that must be
  NULL when unclassified, so mixing conventions inside one block makes T3.5's queue query depend on
  knowing which applies per column. (DJ001 never fired on `identity.email`/`phone` because it exempts
  `unique=True` — worth knowing before "fixing" it.)
- ⚠️ **`author` is `PROTECT`, not `CASCADE`** (C-14): BR-33 implements deletion as anonymization, so
  the cascade should never fire, and `PROTECT` makes a hard-delete attempt fail loudly instead of
  quietly erasing the reports that give an Issue its corroboration count (FR-16).
- ✅ **T4.1 replaced the deliberately absent Issue placeholder with a nullable, protected FK.**
  It enforces BR-6's at-most-one-Issue rule without inventing a loose UUID column, while preserving
  the valid pre-clustering state and preventing a hard delete from erasing citizen evidence.
- ⚠️ **A registered-but-unverified citizen may submit.** BR-30 gates *notification* on verification,
  not intake, and the limited capability set for unverified accounts is explicitly unspecified
  ("don't invent it"). Someone who just registered can still report a live hazard.
- ⚠️ **`create_report()` takes no `status` parameter, and the test asserts the signature.** Testing it
  by passing one would only prove that call was rejected; the guarantee is that the parameter cannot
  exist, or a caller could mark a report `triaged` and skip the whole pipeline.
- ⚠️ **`ReportFactory` builds through `objects.create()`, bypassing `create_report()`, and builds
  *unclassified*.** Routing fixtures through the service would make a validation bug fail unrelated
  suites and force every test to seed a boundary first. Unclassified because BR-9 says a Report
  validly exists before classification — a pre-filled factory makes the async path untestable from
  its real starting state, and every "worker picks up unclassified work" query returns nothing while
  looking correct.
- ⚠️ **`CityBoundaryFactory` defaults `is_active = False`, unlike the model.** The migration already
  seeds one active row and `active_city_boundary()` raises on two, so an `is_active=True` default
  would break every test that merely needs *a* boundary — and the failure would look like a selector
  bug rather than a fixture bug.
- ⚠️ **Two mypy settings were added for `factory_boy`, both narrowly scoped**, matching the existing
  Celery-decorator precedent. `untyped_calls_exclude = ["factory"]` because the declaration helpers
  (`Sequence`, `SubFactory`, `LazyFunction`, `LazyAttribute`) are unannotated despite the package
  shipping `py.typed`; `implicit_reexport` for `factory.*` because `factory/__init__.py` declares no
  `__all__` (verified against the installed package, not assumed). **Neither excuses the `F()`
  shorthand** — `FactoryMetaClass.__call__` is unannotated, so `UserFactory()` types as the *factory*
  and silently defeats checking on every fixture. All call sites use `F.create()` / `F.build()`,
  which are annotated `-> T`; that one change cleared 92 of 118 errors.
- ⚠️ **The P4 concurrency test's `_seed_*` helpers were de-rotted, and mypy forced it.** A
  `# type: ignore[attr-defined]` on its `Report` import became an *unused-ignore* error the moment
  T2.1 defined the model — the ignore comment worked exactly as a self-cleaning device should. The
  T4.4 removed the final import ignore and strict xfail; the same test now runs the real service.
- ⚠️ **The deliberate `author=None` test keeps a `type: ignore[misc]`, and that is the point.** mypy
  rejecting it is the first line of defence; the test asserts the second, because typing does not
  cover raw SQL, a data migration, or `**kwargs` assembled from a request body.
- **No spec amendment owed.** API §6.3 already specifies `OUT_OF_CITY`, the category hint, and the
  `-createdAt` default order; T2.1 ships no endpoint, so nothing diverges.
- ❓ **Unchanged and still open:** Q6 (EXIF default) lands with T2.5 — BR-4 already fixes the
  authoritative location as the explicit report coordinate, never photo EXIF, so nothing here
  depends on it. Q10 (accuracy bar) is why `confidence` stores a value and encodes no threshold.

### T2.2 — `POST /reports`: the fast write + the deferred enqueue (P2, FR-5, NFR-3, API §6.3)

Built the endpoint on top of T2.1's rules: `classification/tasks.py` (`CLASSIFY_REPORT_TASK`,
`classify_report` — the stub T3.5 fills), `submit_report()` in `reporting/services.py`,
`reporting/serializers.py` (`LocationSerializer`, `ReportSubmitSerializer`,
`ReportSubmitResponseSerializer`), `reporting/views.py` (`ReportSubmitView`),
`path("reports", ...)` in `api/urls.py`, and a fix to the T0.6 camelCase layer
(`run_validation` + `_camelize_error_detail` in `api/serializers.py`). 4 tests in
`classification/tests/test_tasks.py`, 37 in `reporting/tests/test_submission.py`, 5 rewritten/added
in `api/tests/test_serializers.py`. **No migration.** Full suite **491 passed / 2 xfailed** (up from
454 / 1), mypy (129 files) / ruff clean, no drift, `check --deploy` clean on `prod`.

- ⚠️ **The whole task is `transaction.on_commit`, and it is asserted from the failing side.** An
  inline `.delay()` publishes the instant it runs, so an idle worker can `SELECT` the report before
  the transaction commits, read nothing, and never triage it. The window is small, real and
  load-dependent, so it survives every local run and appears under production concurrency.
  `test_the_task_is_not_published_before_the_transaction_commits` fails the moment someone removes
  the wrapper — `pytest-django`'s never-committed transaction means a correct callback has *not* run
  when the test returns. Its pair, `..._is_published_once_the_transaction_commits`, is what stops
  `on_commit(lambda: None)` satisfying the first one.
- ⚠️ **`task_always_eager` is deliberately NOT configured for tests, and that is load-bearing.**
  Eager mode runs the task body synchronously inside the caller's transaction, so a suite running
  eager would **pass with the broken inline `.delay()`** — the "worker" shares the uncommitted
  transaction and sees the row anyway. It converts the exact bug this task prevents into a green
  test. `.delay` is patched instead, at the *use* site (`urbenmend.reporting.services.classify_report
  .delay`): the service holds a module-level reference, so patching the definition site would leave
  its binding untouched and every assertion would read zero calls.
- ⚠️ **Only the id crosses the broker boundary, as a `str`, bound to a local before the closure is
  built.** Capturing `report` puts a whole row (author id, free text, coordinates) into Redis
  (NFR-12) and lets the worker act on a pre-commit snapshot instead of re-reading the committed row.
  A `UUID` serializes fine and arrives as a `str` anyway, which would make the task's annotation a
  lie on the receiving side.
- ⚠️ **`status` moves to `PROCESSING` in a second UPDATE inside the same transaction, not by passing
  a status into `create_report()`.** T2.1 refuses that parameter on purpose — it is the thing that
  would let a caller mark a report `triaged` and skip the pipeline. `update_fields` must list
  `updated_at`: it is `auto_now`, and `save(update_fields=...)` writes only the named columns, so
  omitting it leaves the row reading as never-modified.
- ⚠️ **The Celery task carries an explicit `name=`.** Without it the name follows the module path, so
  moving or renaming the module silently renames the task and orphans messages already queued under
  the old name — `NotRegistered`, in the worker, after the deploy. `@shared_task` is untyped, so
  `.delay()` types as `Any` and a wrong argument at the enqueue site is invisible to mypy;
  `test_tasks.py` asserts the registered name, that it is *not* the module path, and the first
  parameter name, because those are the checks the type checker structurally cannot do.
- ⚠️ **The stub logs a warning and returns `None` — it neither raises nor claims success.** Raising
  turns "not implemented" into retry/alert noise on every submission; returning something truthy
  invites a caller to treat it as a completed classification. Passing silently would be
  indistinguishable from a broken broker.
- ⚠️ **`LocationSerializer.to_point` is a `staticmethod` over a plain mapping.** A nested serializer
  never has `validated_data` populated, so reading `self.validated_data` inside it raises
  `AssertionError` at run time. The `(lng, lat)` argument order lives in exactly one place because
  transposing it silently rejects every real submission as out-of-city.
- ⚠️ **`lat`/`lng` carry degree bounds.** Without them `lat: 200` builds a valid `Point`, misses the
  boundary, and answers `422 OUT_OF_CITY` — sending a client with transposed arguments off to debug
  the wrong thing. A test pins `400`, not `422`.
- ⚠️ **`mediaIds` is declared and *refused*, not dropped** (`400`, `details[].field == "mediaIds"`).
  DRF's default would silently discard the key, so a photo-only submission would fail BR-3 with an
  error about `description` — a field the client left empty on purpose, with no way to act on it.
  T2.6 wires the real count; `media_count=0` is the truthful value until then.
- ⚠️ **`ReportSubmitSerializer` is a plain `Serializer`, never a `ModelSerializer` over `Report`.**
  Otherwise `status`, `severity_signal`, `confidence`, `classification_source` and `classified_at`
  are one `fields` edit away from being client-settable — derived data is read-only to all clients
  (C-10, BR-22/24). Same reasoning as T1.9's `ProfileUpdateSerializer`. Unknown fields are rejected
  rather than ignored, in both spellings.
- ⚠️ **The response reads `status` off the row instead of hardcoding `"processing"`.** A hardcoded
  string keeps reporting success after the transition above is moved, removed, or fails.
- ⚠️ **`202`, not `201`, and no `Location` header.** The row is durable, but the resource is
  incomplete — category, severity signal and Issue link are empty until the worker finishes (Arch
  §4), and `GET /reports/{id}` is T2.7, so the header would point at a route that answers `404`.
- ⚠️ **No role check in `permission_classes`.** `IsAuthenticated` establishes *who*; Citizen-only is
  `author.role != Role.CITIZEN` inside `create_report()` (FR-3). Nothing in the view catches
  `ReportValidationError` or `OutOfCity` either — the handler already renders them `400` and `422`,
  and a local `except` would only be a chance to collapse the distinction T2.1 built two types for.
- ⚠️ **A found bug in the T0.6 camelCase layer, fixed here: rejected field names came back in
  `snake_case`.** DRF raises with its *own* field names, so a rejected `mediaIds` was reported as
  `media_ids` — a field the client never sent and cannot map to an input. §4.2 defines
  `details[].field` as naming the client's own submitted field, so the inbound rename was only half
  the boundary. Wrapped at **`run_validation`**, not `to_internal_value`: DRF runs field checks *and*
  `validate()` from inside it, so wrapping the narrower method would leave object-level errors
  untranslated — a half-fix that passes every field-level test. Leaves are returned untouched
  because `ErrorDetail` is a `str` subclass carrying `.code`, which the envelope reads as `issue`;
  rebuilding leaves as plain `str` would degrade every `issue` to the `INVALID` fallback.
- ⚠️ **This endpoint ships unthrottled, deliberately, with a task against it.** FR-33 submission rate
  limiting is **T2.9**. T1.8's buckets are sized for credential guessing and are the wrong shape:
  `auth_user` at 20/15m would cut off a citizen filing several potholes on one walk, and reusing it
  would let report submissions exhaust the login allowance. Naming the gap beats borrowing a limit
  that does not fit.
- ⚠️ **A CSRF test guards the T1.4 note** that any future view setting `authentication_classes = []`
  drops enforcement with no warning. This view does not, and the test is what fails if that changes.
- **Idempotency (BR-5) is T2.3's, and `test_submission.py` carries the `xfail(strict=True)` test that
  makes it pass** (the A10 precedent). Today two identical submissions create two Reports, which
  cluster into one Issue and inflate the corroboration count FR-16 reads as "how many people are
  affected". `strict=True` means the fix cannot ship with a stale marker hiding whether it worked.
- **A correction to the T2.1 record above:** an unknown or retired category slug renders as
  **`400 VALIDATION_FAILED`**, not the `422` that bullet claimed. §6.3 lists the taxonomy check among
  its validation rules and names `422` for `OUT_OF_CITY` alone. The behaviour (refused, never coerced
  to `Other`) was always correct; only the documented status code was wrong.
- **No spec amendment owed.** §6.3 already fixes the `202`, the `Idempotency-Key` header, the hint
  semantics and the error set; the request and response shapes match it as written.
- ❓ **Unchanged and still open:** Q6 (EXIF default) gates T2.5, and BR-4 already fixes the
  authoritative location as the explicit coordinate, so nothing here depends on it. Q9 (LLM provider)
  is why `classify_report`'s body is a stub rather than a provider call.

### T2.3 — `Idempotency-Key` for submission (P2, BR-5, API §4.6)

New `urbenmend/api/idempotency.py` (`normalize_key`, `fingerprint`, `reserve`/`complete`/`release`,
`Reservation`/`Replay`); `IdempotencyKeyReused` + `IdempotencyInProgress` in `api/exceptions.py`;
three `IDEMPOTENCY_*` settings in `settings/base.py`; `SubmissionAcknowledgement`,
`IDEMPOTENCY_SCOPE`, `_acknowledge()`, `_require_citizen()` and `_submission_fingerprint()` in
`reporting/services.py`, with `submit_report()` rewritten around them;
`ReportSubmitResponseSerializer` re-pointed at the dataclass; `ReportSubmitView` reads the header and
sets `Idempotency-Replayed`. 23 tests in the new `api/tests/test_idempotency.py`, 15 added to
`reporting/tests/test_submission.py` (whose `xfail(strict=True)` marker is deleted). **No migration.**
Full suite **529 passed / 1 xfailed** (up from 491 / 2), mypy (131 files) / ruff (139 files) clean, no
drift, `check --deploy` clean on `prod`.

- ⚠️ **API §4.6 was amended first** (the rule, followed), and the amendment settled a real ambiguity:
  §4.6 spoke of replaying "the original response" while §4.2's `409` row listed "idempotency replay",
  which reads as *replay is an error*. It is not — a replay re-serves the original **`202`** and body
  verbatim. `409` is now scoped to the two cases that genuinely conflict: the same key with a
  *different* payload (`IDEMPOTENCY_KEY_REUSED`) and a key whose original has not finished
  (`IDEMPOTENCY_IN_PROGRESS`). This also corrects §C3's stale "returns the original `201` result"
  above.
- ⚠️ **`cache.add()`, never `get()` then `set()` — this single line is the whole concurrency claim.**
  `add()` is Redis `SETNX`: exactly one of N simultaneous callers gets `True`. A read-then-write pair
  reads as equivalent and is not; both callers find an empty slot, both reserve, both write a Report,
  and BR-5's double-tap ships as two rows. `test_only_one_of_two_simultaneous_callers_reserves_the_key`
  is R-2's mandated mitigation — 8 real threads through a `threading.Barrier`, asserting exactly one
  reservation and seven `IDEMPOTENCY_IN_PROGRESS`. **The barrier is what makes it a race**: without it
  the threads serialize and the test passes against the broken implementation.
- ⚠️ **That test runs against real Redis and touches no ORM, deliberately.** Overriding `CACHES` to
  locmem would assert an atomicity guarantee the deployed system does not get from the same code
  path — `add()`'s atomicity is a property of the *backend*. And two threads sharing a
  `pytest-django` test transaction would deadlock long before reaching the interesting line, which is
  why the concurrency proof lives at the store and the endpoint tests are deterministic instead.
- ⚠️ **`complete()` runs from `transaction.on_commit`, never inline.** A completed record is a promise
  that a row exists; writing it before commit means a rollback leaves a record that replays a
  `reportId` for a row which never persisted — and it does so for the entire retention window, to a
  client with no way to detect it. The visible consequence is that a concurrent double-tap gets
  `409 IDEMPOTENCY_IN_PROGRESS` rather than a premature replay. **That is the guarantee, not an
  artifact.** In the tests, the placement of `captureOnCommitCallbacks(execute=True)` *is* the
  assertion: inside the block the original is still in flight, outside it has completed.
- ⚠️ **A failed request does not consume its key** (§4.6). `release()` is called from a broad
  `except Exception`, so a `400`, a `422` and a database error all free the key — the client's next
  move after a validation failure is to fix the body and retry, which is a *different* fingerprint,
  and a consumed key would answer that correction with `409 IDEMPOTENCY_KEY_REUSED` and no way
  forward. Asserted with a corrected body precisely so a `release()` that only downgraded the record
  would still fail.
- ⚠️ **`submit_report()` returns a `SubmissionAcknowledgement`, not a `Report` — and that is what makes
  "verbatim" true by construction.** §6.3 says a replay returns *this same `202` body*. A replay has
  no live row to render: what it has is the acceptance record the original produced. Serializing a
  `Report` on the fresh path and a stored dict on the replay path would put the two bodies in two
  pieces of code, free to drift, with the client-visible symptom being a retry that looks like a
  *different* submission. One dataclass through one serializer.
- ⚠️ **The derivations moved into `_acknowledge()` because they must be evaluated at acceptance
  time.** `issueId` (null in this pre-worker snapshot), `classification.state` (from
  `is_classified`, BR-9) and
  `status` (read off the row) used to be `SerializerMethodField`s. By the time a replay arrives,
  triage may have finished — re-deriving then would answer a `202` with post-acceptance state that
  §6.3 sends clients to `GET /reports/{id}` for. A test mutates the row to `triaged`/`high`/classified
  between the two calls and asserts the replay still reports the original.
- ⚠️ **The fingerprint is checked *before* the state, and both states must agree.** A key bound to
  different content is `409 IDEMPOTENCY_KEY_REUSED` whether the original finished or not: the remedy
  (use a new key) is identical, and reporting an in-flight mismatch as `IN_PROGRESS` would tell the
  client to retry a request that can never be honoured under that key.
- ⚠️ **Nothing identifying reaches Redis** (NFR-12). The cache key is
  `idempotency:<scope>:<sha256(scope|user|key)[:40]>` — the client's key is a bearer token for a
  stored response, and anyone reading it out of `redis-cli KEYS` could replay that response; the same
  reasoning `AuthIdentityRateThrottle` records for hashing an email. The scope stays readable so
  operations can `SCAN` one endpoint. The stored record holds the fingerprint digest and the response
  payload only — never the submission. A test asserts a description does not appear in its own digest.
- ⚠️ **Comparison is on normalized values, not raw bytes** (§4.6). The fingerprint is built from the
  post-validation service arguments — `description`/`address` stripped exactly as `create_report()`
  strips them, the coordinate reduced to the two floats that will be stored. A client that re-sends
  with reordered JSON keys or trailing whitespace is making the same submission; a byte-level
  comparison would answer `409` to an ordinary retry and turn the safety net into a fault. `author` is
  in the payload as well as the cache key — insurance against a collision or a future scope widening.
- ⚠️ **Two TTLs, and the shorter one is not a tuning detail.** `IDEMPOTENCY_IN_PROGRESS_SECONDS` (60)
  is the only backstop for a process killed between `reserve()` and `complete()`/`release()` — and for
  a caller-wrapped transaction that rolls back *after* `submit_report()` returns. At retention length
  (86 400) one crash would lock a citizen's key for a day.
- ⚠️ **The retention window is deployment configuration, and the spec says so rather than fixing a
  number.** `api-conventions.md` lists "idempotency-key retention window" under *not specified — do
  not invent*. The settings carry the numbers with the T1.2/T1.8 "our policy, not spec-derived"
  banner; §4.6 tells clients not to depend on a duration. Also recorded there: a cache flush drops
  every held key, so replays become fresh submissions — a real operational consequence of `default`
  being the same Redis the throttles use.
- ⚠️ **A vanished or unreadable record reads as a fresh claim, not a `409`.** `add()` can lose to a
  record whose TTL expires a microsecond before the read, and a deploy that changes the stored shape
  would otherwise turn every in-flight key into an `AttributeError` — a `500` on submission, for every
  retrying client, until the window drained. Nobody holds the key in either case, so rejecting a
  legitimate submission is strictly worse than one duplicate in a race already outside retention. The
  re-claim goes through `set()`, not a bare pass: without it the caller holds no record and a
  concurrent duplicate would also find an empty slot.
- ⚠️ **Authorization runs before the key is examined**, extending T2.1's observable ordering. A
  non-Citizen gets `403` and learns nothing about whether that key is held — a test asserts the
  authority path never reaches the store.
- ⚠️ **An absent or blank key means no de-duplication, and `""` must not become a key.** Some clients
  send an empty header for an unset value; treating that as a real key would put every such client in
  one bucket and hand the second caller the first one's response. Inferring a key from the body was
  rejected outright: two identical reports from one citizen are two corroborating voices (FR-16), and
  the client is the only party that knows whether a resend is a retry or a second pothole.
- ⚠️ **An over-long key raises rather than truncating.** Truncation aliases two distinct keys onto one
  record — the aliasing bug, politely. It raises Django's `ValidationError`, so the handler renders
  §4.6's `400 VALIDATION_FAILED` without this module importing DRF; the bound is read at call time,
  not bound at import, or `override_settings` cannot reach it (the T1.8 trap).
- ⚠️ **`IDEMPOTENCY_SCOPE = "reports.submit"` is a stored cache-key component.** Renaming it frees
  every key currently held, so every in-flight retry becomes a fresh submission. It lives in `api/`
  rather than `reporting/` because §4.6 governs "other duplicate-sensitive creates" too — T5.x
  confirmations and assignment will reserve through the same store.
- **`Idempotency-Replayed: true` is set only on a replay**, never as a `false` on the fresh path. §4.6
  describes it as additive and as the *only* way a client or an operator reading a log can tell a
  re-delivered acknowledgement from a first one — by design the bodies are identical. `replayed` is
  therefore deliberately **not** declared on the response serializer; a test asserts the exact key set,
  because a leaked fifth key would make the header redundant and the "verbatim" guarantee false.
- **No new migration, and none was expected** — the whole mechanism is Redis-resident. A durable
  idempotency table was considered and rejected: it would need its own retention sweep, and the
  atomicity `add()` gets free would become a `SELECT FOR UPDATE` in the submission's hot path.
- ❓ **Unchanged and still open:** Q9 (LLM provider) — the enqueue this task now de-duplicates still
  reaches a stub. Q6 (EXIF) gates T2.5. FR-33 submission throttling remains **T2.9**; idempotency
  bounds *accidental* duplication, not abuse, and must not be mistaken for a rate limit.

### T2.4 + T2.5 + T2.6 — photo upload, EXIF/derivatives, attach to report (P2, FR-7, P3, BR-3/4)

**Built as one batch** (the owner's call: gates run once per batch, not once per task). The three
tasks are one pipeline — a `Media` row with no upload endpoint, an EXIF policy with no row to apply
it to, and an attach step with nothing to attach are not independently shippable.

- ⚠️ **✅ ❓Q6 RESOLVED (2026-08-08, by the project owner): strip all EXIF, always.** No per-user
  setting, no retain branch, no admin toggle. BR-4 already makes the report's explicit coordinate
  authoritative, so photo GPS is not merely private — it is *unused*: keeping it would create a
  second, unvalidated location for the same report that nothing reads and P3 has to defend.
- ⚠️ **The strip is SYNCHRONOUS; only the derivatives are deferred. This is the central decision of
  the batch.** §6.4 describes the pipeline as asynchronous and answers `202`, while §C3 above says
  "Never store the original EXIF-bearing file". Both hold at once only if the sanitizing re-encode
  runs *before* the first `storage.save()` — which is what `upload_media()` does. Writing the raw
  upload and stripping in the worker would put a GPS-tagged photo of a citizen's home in object
  storage for as long as the queue is deep, and every retry, dead-letter or worker outage extends
  that window. The step is also already paid for: deciding `415` vs `422` requires a full decode, so
  the pixel buffer is in memory by the time the policy check finishes. What stays in the worker is
  the genuinely expensive part — LANCZOS downscale and thumbnail.
- ⚠️ **`ImageOps.exif_transpose` THEN strip, and the order is invisible in any metadata assertion.**
  The orientation tag lives *in* EXIF, so stripping first discards the instruction that says which
  way is up — and the image is stored rotated, permanently, with nothing left to correct it from.
  Every portrait phone photo is affected. `test_sanitize_applies_orientation_before_stripping_it`
  asserts a 64×48 source with orientation `6` comes out 48×64; the EXIF test passes either way.
- ⚠️ **The EXIF test asserts the fixture carries GPS *before* the strip runs.** Pillow silently drops
  EXIF it cannot serialize, so a fixture that quietly stopped writing the GPS IFD would leave the
  rest of the test passing while proving nothing — the exact shape of a security test that has rotted
  into a no-op. (The fixture needs `IFDRational` values, not `(num, den)` tuples: `Exif.tobytes()`
  calls `abs()` while writing and a plain tuple raises `TypeError` there.)
- ⚠️ **A fresh `save()` from a decoded image *is* the strip.** There is no `Image.strip_exif()`, and
  deleting keys out of `image.info` does not help — `save()` re-emits whatever the plugin finds there
  plus whatever the source carried. Writing the pixel buffer to a new buffer with no `exif=` and no
  `icc_profile=` is the operation: the output has no EXIF because nothing ever put any in it.
- ⚠️ **A transparent PNG is composited onto white, not `convert("RGB")`d.** The bare convert discards
  the alpha channel and keeps whatever RGB sits underneath, which for a transparent PNG is usually
  black — so a screenshot with a transparent background is stored as a black rectangle. It looks like
  a corrupt upload and is unreachable from the pixels. Asserted on a corner pixel.
- ⚠️ **`_open()` calls `load()`, because `Image.open()` is lazy and is not a validity check.** A file
  truncated halfway through its scan data opens without complaint and fails later, inside whatever
  touches a pixel — surfacing as a `500` from the worker rather than a `422` on the request that
  caused it. `verify()` was rejected as the alternative: Pillow documents that the image must be
  reopened afterwards, so a `verify()`-then-reuse raises on the next operation and invites someone to
  "fix" it by dropping the check.
- ⚠️ **The format allowlist is checked against what Pillow *detects*, never `upload.content_type`.**
  A renamed executable arrives with a perfectly respectable `image/jpeg` on it. SVG is absent
  deliberately: it is a script container, not a raster photo, and Pillow cannot strip anything from it.
- ⚠️ **`413`/`415`/`422` are three distinct answers and the distinctions are actionable.** `413` is
  "this file is too big", `415` is "send a JPEG instead of this", `422` is "this JPEG is damaged,
  retake the photo". One code for the last two leaves a client unable to tell which remedy applies.
  The checks run in **cost order** — size (a length comparison, no decode), then format, then
  decodability — because reversed, a 500 MB file is fully decoded before being refused for its size.
- ⚠️ **`PayloadTooLarge` exists because Django has no bound here at all.**
  `DATA_UPLOAD_MAX_MEMORY_SIZE` explicitly exempts `request.FILES`, so without
  `MEDIA_MAX_UPLOAD_BYTES` a file upload is unbounded. Django's own `RequestDataTooBig` is a
  `SuspiciousOperation` and renders `400`, not §6.4's `413`.
- ⚠️ **The decoder's message never reaches the client.** Pillow quotes header bytes in some errors, so
  a crafted upload could echo chosen content back out through the §4.1 envelope. Operators get the
  real text in the log line, correlated by `traceId` (NFR-12). Same reason `failure_reason` is a
  column but not a serializer field.
- ⚠️ **`Media` is created *unattached*, and that is the contract.** §6.4 uploads and hands back a
  handle; §6.3 then submits with `mediaIds`. During that window `owner` is the only thing
  authorization can key on — which is why it is a column of its own rather than derived from
  `report.author`, and why the FK is nullable.
- ⚠️ **Two-step attach: `resolve_media_for_attachment()` proves, `attach_media()` binds.** BR-3 is
  validated *before* the report row exists (a photo-only submission is only legal if the photos are
  real), so the count has to be established first and the binding cannot happen until there is a
  report to bind to. Merging them means either creating the report on unverified ids or validating
  BR-3 after the write.
- ⚠️ **`report__isnull=True` appears in *both* queries, and it is what makes attachment single-use.**
  Without it a citizen could pass one of their earlier reports' `mediaIds` and mount the same photo
  on ten reports — which FR-16 would then count as ten corroborating voices for one pothole. The
  ownership filter is re-applied on the write too: an authorization check that only runs on the read
  path is not an authorization check (FR-3).
- ⚠️ **`attach_media()` has no `transaction.atomic` of its own — it rides `submit_report()`'s.** A
  nested decorator would create a savepoint that commits independently of the report write, so a
  `422 OUT_OF_CITY` would leave photos attached to a report that was rolled back. A test submits an
  out-of-city location and asserts the media are still unattached.
- ⚠️ **Every id must resolve, or the whole submission is refused.** Silently dropping an unresolvable
  id makes a photo-only submission fail BR-3 with "describe the problem" — an error about
  `description` when the client did attach a photo, and no way to act on it. That is the failure T2.2
  refused when it declared `mediaIds` rather than letting DRF drop the key.
- ⚠️ **One message for "not yours", "already attached" and "does not exist".** Three distinct messages
  turn this field into an oracle for whether a media id exists and who owns it — T1.5's "`403` to act,
  `404` to see", applied at field level.
- ⚠️ **A still-`PROCESSING` photo counts toward BR-3.** Arch §4.1 says a Report can be usable before
  its Media is `Ready`; requiring `READY` would make BR-3 depend on worker latency, so a citizen who
  submits a second after uploading is told their photo does not exist. Duplicated ids collapse rather
  than raising — a client sending the same id twice is buggy, not abusive.
- ⚠️ **The idempotency fingerprint now hashes `sorted(media_ids)`, not the count.** Two submissions
  with the same description and different photos are different submissions; a count-based fingerprint
  would replay the first one's acknowledgement for the second.
- ⚠️ **`READY` describes the *derivatives*, not the safety of the original** — the stored master is
  already EXIF-free in `UPLOADED`. Reading `READY` as "now it is safe to serve" inverts the pipeline
  and implies the earlier states are not.
- ⚠️ **`FAILED` is not a delete and the task does not raise.** data-model §4 marks the branch "retry",
  and the retry is an operator action against a durable state: the common causes (a permanently
  undecodable master, a storage misconfiguration) would be hammered forever by an automatic redelivery
  while the queue backs up behind them (O-2). A row that vanished on a transient error would also take
  its Report's BR-3 justification with it — a report valid at submission would retroactively have
  neither photo nor adequate description.
- ⚠️ **The worker re-reads the row and re-checks its state, never trusting the id.** At-least-once
  delivery means it runs twice for one photo; the state guard is what stops a second downscale
  compressing an already-downscaled master (a photo redelivered three times would visibly degrade with
  nothing to explain why). It also lets **moderation win the race**: a photo removed between enqueue
  and execution is left alone rather than flipped back to `READY`.
- ⚠️ **`REMOVED` is how moderation deletes** (FR-31, database.md). The read answers `410 GONE`, which
  is only expressible while the row still exists — `404` would leave a client retrying forever and
  erase the fact that moderation acted, while `410` for an id that never existed would confirm to a
  scanner that the id had once been valid. **The stored objects stay in the bucket**: FR-31 is
  moderation, not erasure, and deleting the `FieldFile` would run outside the transaction, so a
  rollback would leave a live row pointing at nothing.
- ⚠️ **An Admin moderating is deliberately NOT bound by the author's pre-triage lock.** An author gets
  `409 NOT_EDITABLE` once triage has run — a photo an Authority has been dispatched on must not vanish
  from underneath them — but an Admin held to that lock could not remove anything that had been
  triaged, which is precisely the content a takedown request is about.
- ⚠️ **`MediaDetailView` uses `AllowAny` and does NOT empty `authentication_classes`.** Setting it to
  `[]` is what silently disables CSRF (the T1.3 trap `identity/tests/test_csrf.py` exists to catch),
  and this view has a mutating method. `AllowAny` skips the *permission* check while
  `SessionAuthentication` still runs, so an authenticated `DELETE` stays CSRF-protected and an
  anonymous `GET` still works. The `DELETE` checks authentication itself so an anonymous caller gets
  `401` rather than an `AttributeError` from `AnonymousUser.role`.
- ⚠️ **`url`/`thumbnailUrl` are `SerializerMethodField`s, not `FileField`s.** A DRF `FileField` renders
  `.url`, which raises `ValueError` on an empty file — so §6.4's `202` body, where the thumbnail does
  not exist yet, would be a `500` instead of the documented `null`.
- ⚠️ **`MediaResponseSerializer` is not a `ModelSerializer`.** The neighbouring columns are `owner`,
  `report`, `byte_size` and `failure_reason`; an owner id is another citizen's identifier and
  `failure_reason` carries decoder text. One `"__all__"` publishes all four. A test asserts the exact
  key set.
- ⚠️ **A pre-existing §4.1 contract bug was found and fixed here, and it was never media-specific.**
  The T0.6 handler rendered a service-raised `ValidationError` via `exc.messages`, which **flattens a
  mapping to a bare list of its values** — so every dict-shaped service error produced a `details`
  entry with *no `field` key at all*: right status, no way to know which input to fix. It went
  unnoticed because the field name in T2.2's tests came from the *serializer*, where the camelCase
  layer supplies it. Now `exc.message_dict` guarded by `hasattr(exc, "error_dict")` — the documented
  discriminator, because reading `.message_dict` on a list-shaped error raises `AttributeError` *from
  inside the error handler*. Keys are camelCased **at the handler**, since no serializer is in the loop
  for a service-raised error and `media_ids` would otherwise reach a contract that says `mediaIds`.
  `_camelize_error_detail` became public `camelize_error_detail` for the cross-module call.
- ⚠️ **`conftest.py` swaps `STORAGES["default"]` for `InMemoryStorage` session-wide**, for the same
  class of reason `_reset_throttle_cache` exists: storage is not part of the test transaction. The
  default backend is `S3Boto3Storage`, so the moment a `Media` fixture existed, saving one opened a
  real connection to MinIO and — with no credentials in the test environment — botocore fell through
  to an **EC2 instance-metadata lookup** and failed with `NoCredentialsError` after a timeout. It is a
  real `Storage`, not a mock, so `media.file.save(...)` and `stored.url` run their genuine paths; and
  it is overridden in `conftest.py` rather than `settings/dev.py`, where flipping it would make the
  running dev server silently lose every photo on restart.
- ⚠️ **`MediaFactory` passes `filename=".jpg"` — the extension alone — because that is what
  `upload_media()` passes.** It reaches `_upload_path()` as the `filename` argument and is
  concatenated onto `"original"`, so a full `"original.jpg"` produces `reports/<id>/originaloriginal.jpg`:
  a fixture whose storage key does not have production's shape, which is the one thing a fixture
  holding a real file is for.
- ⚠️ **`urbenmend.media.tasks` was added to the `disallow_untyped_decorators = false` override list**,
  not the rule widened — the T2.2 instruction, followed. Consequence at the call site is unchanged:
  `process_media.delay(...)` types as `Any`, so `test_tasks.py` asserts the task name instead.
- **`MEDIA_MAX_UPLOAD_BYTES` (10 MiB), `MEDIA_MAX_DIMENSION` (2048), `MEDIA_THUMBNAIL_DIMENSION`
  (320), `MEDIA_IMAGE_QUALITY` (82), `MEDIA_MAX_PER_REPORT` (5), `MEDIA_ALLOWED_IMAGE_FORMATS` are
  our numbers, not spec-derived.** FR-7 says "enforce size/type limits" and §6.4 names the statuses
  without fixing a value; they carry the T1.2/T1.8 banner and are env-overridable (NFR-11). Raise them
  into `docs/04-api-specification.md` if any ever becomes a contract.
- **One migration, `media/0001_initial`, verified reversible** (`migrate media zero` → `migrate media`).
  No new geometry, so no `CreateExtension` dependency is involved.
- ❓ **Unchanged and still open:** Q9 (LLM provider), Q3, Q5, Q10. **`POST /media` ships unthrottled
  and it is the more attractive target of the two** — each request costs a full decode and a storage
  write — so **T2.9 must cover this route, not only `POST /reports`**. The T1.8 auth buckets are the
  wrong shape: sized for credential guessing, they would cut off a citizen photographing one flooded
  street.

### T2.7 + T2.8 + T2.9 — the reads, the edit, and submission rate limiting (P2, FR-11/FR-33, API §6.3/§4.5)

**Built as one batch**, the second in P2 (the owner's call: gates run once per batch). These three are
one endpoint's remaining surface — `GET /reports` needs the resource serializer that `PATCH` answers
with, `PATCH` needs the `404`/`410` selector the read introduced, and a write endpoint that FR-33
leaves unlimited is what T2.2 and T2.4 both shipped with a note against.

**T2.7 — `GET /reports` and `GET /reports/{id}`**

- ⚠️ **One view class for both verbs on `/reports`, and `ReportSubmitView` was renamed
  `ReportCollectionView`.** §6.3 gives `POST` and `GET` the same URL and two `APIView` subclasses on
  one `path()` is not expressible. The route *name* stayed `reports` — `reverse("api:reports")` is
  already in T2.2's tests and in clients' generated code, so renaming it would break callers to record
  a fact the class name already carries. (Two references to the old class name survive in the T2.2
  record above; they are historical prose about what existed then and are correct as written.)
- ⚠️ **The detail read is public and the collection is not** — and that is not an inconsistency. Q7
  resolved report *visibility* as public, but "all reports" has no meaning without a caller: §6.3
  scopes the list to own / in-scope / all by role, so an anonymous list would have to mean "every
  report in the city", which is a different endpoint nobody specified.
- ⚠️ **`404` for absent, `410` for moderated, decided in `get_report_for_read()` and shared by both
  verbs.** A `404` for a hidden report would be a disclosure choice made twice, in two places, and
  api-conventions.md fixes `410` for content removed by moderation (FR-31). `HIDDEN` answers `410`
  too — the outcomes differ in what an operator can still recover, not in what a client is told.
- ⚠️ **Visibility is resolved before any filter is applied**, in the selector, for all three roles.
  A filter applied first and a scope applied second is the same query today and a leak the moment
  someone adds a `.union()` or an early `.values()`.
- ⚠️ **An Authority's scope is over `category`, so an unclassified report is in nobody's scope** — the
  un-triaged backlog belongs to T3.5's worker, not to a department. `scoped_category_ids()` returning
  `None` still means Admin/unrestricted, the T1.5 convention.
- ⚠️ **Moderated rows are excluded for every role, Admin included.** The moderation review surface is
  §6.13's; a hidden row appearing in an ordinary list is how a removed photo gets re-shared by someone
  who never saw the removal.
- ⚠️ **`visible_media_prefetch()` is a `Prefetch` with `to_attr`, not `prefetch_related("media")`.**
  The plain form prefetches moderated rows too and pushes the exclusion into the serializer, in
  Python, where it is invisible to anyone reading the query. `to_attr` leaves `report.media` alone, so
  a caller that ignores the prefetch gets the ordinary manager rather than a silently-filtered one —
  and the attribute name is a shared constant (`VISIBLE_MEDIA_ATTR`) because a serializer reading one
  spelling and a selector writing another would not fail, it would just read a different list.
- ⚠️ **The cursor's `-pk` tie-break has to flip with the sort direction**, which is the only reason
  `ReportCursorPagination` exists — and the direction is passed in from the view rather than re-parsed
  from `request.query_params`, so the validated value and the cursor cannot disagree. It is constructed
  in the view, not taken from `DEFAULT_PAGINATION_CLASS`: `APIView` has no `self.paginator` to
  configure, and one explicit construction beats a class attribute plus a hook that mutates it.
- ⚠️ **The query serializer runs before the selector and its `400`s are the deliverable.** Reading
  `request.query_params.get()` instead would ignore `?statuss=triaged` and answer `200` with the
  *unfiltered* list — a citizen looking for their open reports shown all of them, with no signal the
  filter was dropped. `reject_unknown_fields()` gained `extra_allowed` for `?limit=`/`?cursor=`, which
  are the paginator's params: a query serializer that refused them would make every second page a
  `400`.
- **`?q=` is `icontains` today, and the record says so rather than implying more.** §1.4 wants
  bilingual search; this is a substring match. `__dwithin` on the `geography` column takes **metres**,
  which is why `radiusM` needs no conversion — on a `geometry` column the same call would silently
  mean degrees.

**T2.8 — `PATCH /reports/{id}`**

- ⚠️ **FR-11 grants two different rights to two different callers through one endpoint, and collapsing
  them into "may this user edit this report?" fails in both directions.** The author may fix
  description and category *until triage*; an official may re-categorize *after* it and may never
  touch the description. One merged check either lets an official rewrite a citizen's account of what
  they saw, or blocks the correction path FR-11 exists for.
- ⚠️ **A human correction must NOT stamp `classified_at`, and this is a constraint on two later
  tasks.** Stamping it looks like an improvement — the worker can no longer revert the official — but
  `is_classified` keys on that column while `severity_signal` stays `NULL`, so the report reads as
  triaged with no severity band and `SEVERITY_RANK[None]` raises inside BR-11's `max()` in T4.6.
  **The protection T3.5 must implement instead: do not overwrite the `category` of a report whose
  `classification_source` is `AUTHORITY`.** `CITIZEN` carries no such protection — §6.3 calls a
  citizen's category "a hint only" and FR-10 has the LLM decide.
- ⚠️ **BR-3 is re-checked on the way out, against `visible_media_count()`.** An edit is the one path
  that can leave a report with neither a photo nor an adequate description — a state `POST /reports`
  cannot produce. The count excludes moderated rows on purpose: an author blanking the description of
  a report whose only photo an Admin removed under FR-31 must be refused, and counting the removed row
  would let the submission end up with no evidence at all.
- ⚠️ **`None` means "not sent" all the way from the view to the service.** A `data.get("description",
  "")` in the view turns every category-only edit into a description blanking — and on a photo-less
  report BR-3 then rejects it, so the visible symptom is a `400` about a field the client never sent.
- ⚠️ **A stranger gets `403`, not `404` — the usual rule inverted, deliberately.** "`403` to act,
  `404` to see" (T1.5) protects existence; here `GET /reports/{id}` is public, so this report's
  existence is not a secret this endpoint could keep and a `404` would be a lie one `GET` disproves.
  §6.3's error list for `PATCH` names `FORBIDDEN`. The message still names neither role nor resource.
- ⚠️ **`409 NOT_EDITABLE`, not the generic `CONFLICT`.** "Already triaged" is a state a client can act
  on — stop offering the edit affordance — while `CONFLICT` is indistinguishable from a duplicate
  submission. The window is `{submitted, processing}`: it closes at *triage*, not at enqueue, because
  a report sits in `processing` for as long as the queue is deep and a citizen who spots their own typo
  one second after submitting must not be told it is too late.
- ⚠️ **No `severity` field on the `PATCH`, and `reject_unknown_fields()` is why sending one fails
  loudly.** Report ≠ Issue: severity lives on the Issue, and FR-11's "re-severity" is §6.5's
  Issue-level Authority override. Dropped silently, `{"severity": "critical"}` would answer `200` and
  the caller would believe it took.
- ⚠️ **An Admin correction records `AUTHORITY`; the enum has no `ADMIN` member on purpose.** PRD §4.2
  gives Admin every Authority capability, so an Admin re-categorizing is doing the Authority's job —
  and a fifth member would divide the "a human official set this" set in two, so every future query
  that must exclude human decisions (T3.5's, above) would need updating and would fail open if it were
  not.
- ⚠️ **An anonymous `PATCH` is `401`, checked in the method.** `AllowAny` sits on the class so the
  public `GET` is reachable, so `request.user` can be `AnonymousUser` here; passing that to
  `update_report()` reads `.role` off a model that has none, i.e. a `500`. **`authentication_classes`
  is not emptied** — that is the T1.3 trap that silently disables CSRF, and this class carries a
  mutating verb.
- ⚠️ **An Authority may move a report *out of* their own scope.** Scope is checked against the report
  as it stands, not against the destination: a one-way door would mean the only officials who can fix
  a mis-categorized report are the ones it was wrongly filed with. An unclassified report is in no
  Authority's scope and so is `403` for all of them; a retired slug is refused `400` rather than
  coerced to `Other` (BR-7's coercion is for LLM output — filing an official's correction under
  `Other` loses the decision they just made).
- ⚠️ **`save(update_fields=...)` must list `updated_at`** — it is `auto_now`, and omitting it leaves
  the row reading as never-modified. The same trap `submit_report()`'s status flip records.
- ⚠️ **FR-11's "corrections are logged" is one structured line, and it carries *both* slugs.** The
  requirement continues "can seed prompt examples / evaluation sets", which needs the pair — what the
  machine said and what the human chose. No description text, before or after: NFR-12 keeps PII out of
  logs, and *that* it changed is the auditable fact. The immutable store is still T8.1.

**T2.9 — submission rate limiting**

- ⚠️ **A second settings dict, `SUBMISSION_THROTTLE_RATES`, merged by scope name in `get_rate()`.**
  Adding `submit_*` scopes to `AUTH_THROTTLE_RATES` would leave ops tuning spam control under a name
  that says authentication, and renaming that dict is churn across settings, `.env.example` and
  `docs/09-operations.md`. **The merge is asserted in both directions**, because each is silently
  wrong in only one: a version reading only the submission dict breaks login backoff, and a version
  reading only the auth dict passes every submission test by falling through to the module defaults
  and then ignores the operator's tuning forever.
- ⚠️ **`POST /media` is limited, which closes the note T2.4 left.** It is the more attractive target
  of the two — each request costs a full decode and a storage write. The T1.8 auth buckets were the
  wrong shape for it, as that note predicted.
- ⚠️ **The per-IP bucket is shared by both `POST`s, and that is the Sybil control** (PRD §T3). A farm
  of fresh accounts gets a fresh per-account allowance with every registration; the source address is
  the thing that does not rotate for free. The consequence — a five-photo report spends six units of
  one limit rather than one unit of two independent ones — is documented in §4.5 so a client can
  explain a `429` on `POST /reports` caused by its own uploads.
- ⚠️ **Two per-account scopes, not one shared `submit_user`.** A single bucket would have to be sized
  for the photo-heavy case, which then hands text-only spam six times the budget it should have.
- ⚠️ **The `GET`/`POST` split is `get_throttles()`, not `throttle_classes`** — the latter applies to
  every method on a view, so the obvious wiring throttles the read too, and nothing errors when it
  does: the map and the Authority queue simply start answering `429` to whoever is using the product
  most. Returning `[]` for `GET` also suppresses the `RateLimit-*` headers on it, deliberately —
  advertising a limit an endpoint does not enforce makes a client back off from a read it is free to
  make. (`self.request`, not an argument: DRF calls `get_throttles()` with none.)
- ⚠️ **A rejected request consumes its bucket, and so does an idempotent replay.** Both are documented
  in §4.5 rather than exempted. Refunding failures is the natural-looking fix and it leaves the limit
  applying only to legitimate use; exempting a replay would mean doing the §4.6 lookup inside a
  throttle's `get_cache_key()`, i.e. moving idempotency into the throttle layer to save a client that
  is already far below the limit. `RateLimit-Remaining` is the signal instead.
- ⚠️ **The per-account throttles return `None` for an anonymous caller rather than DRF's IP
  fallback**, which would file an anonymous request under a scope named for accounts and advertise a
  per-account allowance to a caller with no account. Safe because `check_permissions()` runs before
  `check_throttles()` — verified against DRF's installed `APIView.initial`, then asserted end-to-end
  (three `401`s cost no budget). ⚠️ **That claim holds only because both endpoints are
  `IsAuthenticated`**; a future public write on either view breaks it.
- ⚠️ **An unknown scope raises `ImproperlyConfigured` at instantiation.** `SimpleRateThrottle` treats
  a `None` rate as *unlimited*, so a silent fallback turns a typo between the class and the settings
  dict into an endpoint with no limit at all.
- ⚠️ **None of these buckets clears on success**, unlike `clear_identity_throttle()`. A legitimate
  report proves nothing about the next one — volume *is* what FR-33 limits, and the T1.8 reasoning
  (success means the credential was right) does not transfer.
- **`SUBMISSION_THROTTLE_RATE_REPORT` 20/1h, `_MEDIA` 60/1h, `_IP` 120/1h are our numbers**, not
  spec-derived: §4.5 requires "tighter buckets on … report submission" without fixing values. They
  carry the T1.2/T1.8 banner and are env-overridable (NFR-11). The report bucket doubles as the
  per-account LLM cost ceiling (NFR-13). **Media is deliberately not `MEDIA_MAX_PER_REPORT × report`**
  — making the report bucket bind first would leave the expensive endpoint effectively unbounded, so
  60/1h lets the upload bucket bind under photo-heavy load while a single five-photo submission still
  fits inside both. The per-IP bucket is sized for carrier NAT.
- ⚠️ **`docs/04-api-specification.md` was amended in three places before the code shipped**, per the
  spec-first rule. **§4.5:** it said "report submission" and was silent on whether `POST /media` was
  included, on the shared per-IP bucket, on failure-consumes-budget and on replay-consumes-budget — all
  four are now written down, plus a table of which endpoints are limited and a note that the numbers
  are deployment configuration rather than contract. **§6.3:** `mediaIds` was listed without saying
  what makes an id usable, so ownership, single-use, the `MEDIA_MAX_PER_REPORT` cap and
  fail-the-whole-submission were discoverable only by hitting them (the T2.4 record's "raise them into
  the spec if any becomes a contract", acted on — the cap is enforced on `POST /reports`, not on the
  upload, and a client building a photo picker needs it). **§6.4:** FR-7's "enforce size/type limits"
  named no values and left the three rejections indistinguishable; the amendment fixes the size cap,
  the accepted formats, and — the correction worth having — that `415` means *"we recognize this format
  and do not accept it"* while a renamed PDF is `422`, because nothing identified it as an image at
  all. Getting that pair backwards tells a client to convert a file it should re-capture.
- ⚠️ **The suite lives in `api/tests/`**, not in `reporting/` or `media/`: the shared per-IP bucket is
  a rule about the two endpoints *together*, and asserted from one app's suite it would only ever be
  half-tested. Its `_rates()` helper sets the two buckets it is not testing **out of reach**, which is
  the point and not padding — every `Client()` reports `127.0.0.1`, so a test that tightened
  `submit_report` and left `submit_ip` at its default could pass by tripping the wrong bucket, and
  would keep passing if the per-account bucket were deleted outright.

**What the once-per-batch gate caught, recorded because it is the argument for the cadence**

- **Five mypy errors, all in this batch's own new code.** `Prefetch` is generic in three parameters
  under `disallow_any_generics` (`Prefetch[str, QuerySet[Media], str]`); `reject_unknown_fields()` had
  been widened to `BaseSerializer[Any]`, which has no `fields` (it is declared on `Serializer`, and
  the wider annotation reads as accommodating while being simply wrong); and
  `report.category.slug if report.category_id else None` appears three times and cannot narrow — the
  `category_id` spelling *looks* like the cheap one but is identical in query count, since Django
  answers `None` for a `NULL` FK without touching the database. All three now bind the related object
  to a local.
- **One stale test.** `test_get_reports_is_405_until_t27` was T2.2's honest placeholder and T2.7 made
  it false. Replaced by a parametrized `PUT`/`DELETE` → `405`, which keeps the claim that was worth
  asserting: `PUT` is reserved for full replacement (api-conventions.md), so a client sending one has
  misread the API rather than hit a missing feature.
- **No migration in this batch** — `makemigrations --check` clean, and `media/0001` (T2.4) re-verified
  reversible `[X]` → `[ ]` → `[X]`.
- **Suite: 714 passed / 1 xfailed**; mypy 145 files, ruff 153 files, `check --deploy` clean, no drift.
- ❓ **Unchanged and still open:** Q9 (LLM provider — T3.5's blocker, and the report bucket above is
  sized as its cost ceiling), Q3, Q5, Q10. **`CLAUDE.md`'s "Build state" paragraph is stale** (it
  still reads "Next: T2.4" with a 529-test suite) — left untouched on the owner's instruction, and
  noted here so it is not mistaken for the current state.

## C3. P2 Reporting & Media
- T2.2: the `POST /reports` response is `202 Accepted` — the report is written synchronously, triage
  is async. The response body is the created report resource (not empty). Return immediately; do not
  wait for classification.
- T2.3: idempotency key is checked *before* the write, in the service. A replay returns the original
  **`202`** result — corrected from `201` when T2.3 shipped: `POST /reports` answers `202` (§6.3), and
  a replay that returned a different status from the request it replays would not be a replay. Key is
  scoped per user **and per operation**, stored in Redis.
- T2.5: EXIF orientation must be applied *before* stripping — Pillow's `ImageOps.exif_transpose`
  then strip all EXIF. Never store the original EXIF-bearing file.
- T2.1: boundary check uses PostGIS point-in-polygon (`ST_Within` or `dwithin` against the city
  polygon). Reject with `422 OUT_OF_CITY` if outside.
- T2.7: `GET /reports/{id}` is public (Q7); `GET /reports` is not — the list is scoped to own /
  in-scope / all by role, so it needs a caller to mean anything. Moderated content answers `410`, for
  every role.
- T2.8: `PATCH /reports/{id}` carries **no severity field** — severity lives on the Issue, and FR-11's
  re-severity is §6.5's Issue-level override. The author's edit window closes at triage; an official
  may re-categorize after it but never touches the description.
- T2.9: FR-33 covers **both** write endpoints, `POST /reports` and `POST /media`, per account *and*
  per source address. The per-source bucket is shared between them — that is the Sybil limit, and it
  is why a five-photo report costs six units of it.

**M2 gate:** report + photo submitted → `202` immediately; photo stored with EXIF stripped; citizen
can retrieve own reports; out-of-city and invalid submissions rejected correctly; duplicate submits
are idempotent.

- [x] Report entity + validation primitives (T2.1 — ⚠️ service is the enforcement point; ⚠️ intake
      fails closed with no boundary configured)
- [x] out-of-city rejected `422 OUT_OF_CITY` (T2.1 — C-11; ⚠️ distinct type from `VALIDATION_FAILED`)
- [x] BR-3 "at least one of {photo, adequate description}" (T2.1 — ⚠️ `media_count` is a parameter
      until T2.4 associates real media)
- [x] report submitted → `202` immediately, triage enqueued (T2.2 — ⚠️ `transaction.on_commit`;
      ⚠️ throttled as of T2.9; the *photo* half of this gate line is T2.4/T2.5)
- [x] duplicate submits are idempotent (T2.3 — ⚠️ `cache.add()` atomicity is the concurrency claim;
      ⚠️ failed requests do not consume their key)
- [x] photo stored with EXIF stripped (T2.4/T2.5 — ✅ ❓Q6 RESOLVED 2026-08-08: strip all EXIF always;
      ⚠️ the strip is **synchronous**, before the first `storage.save()` — only the derivatives are
      deferred; ⚠️ transpose *then* strip)
- [x] photos attach to a report and satisfy BR-3 (T2.6 — ⚠️ two-step resolve-then-bind;
      ⚠️ `report__isnull=True` is what makes attachment single-use)
- [x] citizen can retrieve own reports (T2.7 — ⚠️ visibility resolved in the selector *before* any
      filter; ⚠️ the detail read is public while the collection is session-scoped; ⚠️ moderated
      content is `410` for every role, Admin included)
- [x] a report can be corrected (T2.8 — FR-11; ⚠️ two rights through one endpoint, and a human
      correction must not stamp `classified_at`; ⚠️ BR-3 re-checked against *visible* photos)
- [x] submission is rate limited (T2.9 — FR-33; ⚠️ covers `POST /media` too; ⚠️ the per-source bucket
      is shared by both endpoints; ⚠️ a rejection and a replay each consume budget)

**M2 gate status: all lines met.** The remaining P2 task IDs in `05-project-plan.md` are covered by the
two batch records above; nothing in this gate is outstanding. **P3 (classification) is blocked on
❓Q9** — T3.1's ABC and T3.3's keyword fallback can be built against it, but T3.2's adapter cannot be
finished without a provider.

## C4. P3 Classification
- T3.1: `ClassificationService` is a plain Python ABC with no Django imports. This is what makes it
  unit-testable and provider-swappable. The interface returns `(category, severity_signal,
  confidence, source)`.
- T3.2: the LLM provider is deliberately not chosen yet (Q9 deferred). Build the adapter against the
  ABC; the provider is a settings value. Minimize PII in the prompt (P7).
- T3.4: the cost/rate cap must degrade gracefully — when the cap is hit, fall through to the keyword
  fallback. The submission still returns `202`; triage still completes. **Never block intake.**
- T3.6: circuit breaker on LLM calls. After N consecutive failures, open the circuit and route
  directly to the fallback without attempting the LLM.

**M3 gate:** submitted report is classified asynchronously; LLM unavailable → keyword fallback
produces a result; classification source is recorded; failures degrade, not crash.

**P3 implementation record (2026-08-14):** T3.1-T3.7 are complete. The worker now classifies and
persists category, severity signal, confidence, source, model and rationale; unavailable, malformed,
rate-limited, over-budget and circuit-open LLM paths degrade to the bilingual keyword fallback.
Identical-text LLM results use a privacy-safe Redis cache, provider failures drive a circuit breaker,
and at-least-once task delivery is idempotent. Authority/Admin category corrections are preserved,
while stale text edited during an external call is requeued instead of overwritten. Confidence is
stored for every result; the internal review flag remains disabled until Q10 supplies a threshold.
Validation: 949 passed / 1 xfailed; ruff, format, mypy, model drift and production deploy checks
clean; P3 migrations verified forward/reverse/forward on a fresh database. Next: P4 / T4.1.

## C5. P4 Clustering & Issues
- **T4.1 implementation record (2026-08-14):** complete. `Issue` now owns its controlled primary
  category, representative WGS84 geography point, computed severity and rationale, non-destructive
  authority override metadata, workflow status, optional assignee, duplicate link, and open/update
  timestamps. `Report.issue` is nullable until clustering and `PROTECT`ed, with the reverse member
  set and report count derived from the relationship rather than a counter column. Both admin
  surfaces are read-only for Issue-owned state, and Report reads now expose the attached opaque
  `issueId`. Validation: 959 passed / 1 xfailed; ruff, format, strict mypy, model drift and production
  deploy checks clean; migrations verified forward/reverse/forward on a fresh database and applied
  to development. Next: T4.2 geospatial indexing/setup.
- **T4.2 implementation record (2026-08-14):** complete. `Issue.representative_location` now has
  the named `issues_issue_location_gist` GiST index, added and removed with explicit concurrent SQL
  in a non-atomic reversible migration. This explicit database/state split is required because
  GeoDjango 5.2's spatial-index SQL builder drops the `concurrently=True` argument supplied by
  `AddIndexConcurrently`. Issue selectors now expose metre-based `ST_DWithin` radius filtering and
  GiST-assisted KNN `<->` nearest ordering through the ORM. Real PostGIS tests assert distances,
  query operators, the database index definition, and query plans using the named index.
  Validation: 966 passed / 1 xfailed; ruff, format, strict mypy, model drift and production deploy
  checks clean; the concurrent migration passed forward/reverse/forward on a fresh database and is
  applied to development. Next: T4.3 admin-managed per-category clustering rules.
- **T4.3 implementation record (2026-08-14):** complete. `ClusteringRule` stores a category's
  positive radius in metres, positive time window in hours, active/retired lifecycle and audit
  timestamps. Historical retired versions may coexist, while a partial database uniqueness
  constraint permits exactly one active rule per category. The migration seeds every controlled
  category with a conservative `50 m / 72 h` starting rule: these are explicitly chosen initial
  data based on API §6.10's example and Architecture §4.3's under-merge preference, not hard-coded
  clustering behavior. Django admin can create and tune future-facing rules but cannot hard-delete
  them, and the selector reads the database afresh so an edit affects the next clustering decision
  without restarting workers. Missing configuration fails closed. Validation: 978 passed / 1
  xfailed; ruff, format, strict mypy, model drift and production deploy checks clean; the schema and
  seven-row seed passed forward/reverse/forward on a fresh database and are applied to development.
  Next: T4.4 concurrency-safe find-or-create clustering.
- **T4.4 implementation record (2026-08-14):** complete. `cluster_report(report_id)` now locks the
  Report row for at-least-once idempotency, resolves the live category rule, and takes sorted
  transaction-scoped PostgreSQL advisory locks over the Report's geohash cell plus its eight
  neighbors, with category in every key. Neighbor locking closes the duplicate race across a cell
  boundary; sorted acquisition prevents deadlocks. Inside the same atomic block it selects the
  nearest open same-category Issue within the configured radius and Report-relative time window,
  or creates one, then attaches and triages the Report. Category, distance, age and terminal-status
  mismatches create distinct Issues; missing, malformed, moderated and unclassified Reports fail
  explicitly. Existing-Issue severity recomputation remains T4.6; worker chaining was deliberately
  deferred to T4.5.
  Validation: 997 passed with no xfails; ruff, format, strict mypy, model drift and production deploy
  checks clean. The live two-thread race test passed in the full suite and ten consecutive stress
  runs. Next: T4.5 clustering worker step after classification.
- **T4.5 implementation record (2026-08-14):** complete. The existing stable Celery triage task now
  runs classification first and invokes clustering only after `classified` or
  `already_classified`; stale input is requeued, while missing, moderated and ineligible Reports
  remain safe no-ops. Clustering failures intentionally propagate so task delivery can retry. The
  completed classification is durable, therefore redelivery returns `already_classified` and
  retries clustering without another provider call. Worker-level tests prove the ordering, the
  successful `processing` → `triaged` transition, nearby Reports converging on one Issue, and
  already-classified Reports clustering without another LLM call. Validation: 1007 passed with no
  xfails; ruff, format, strict mypy, model drift and production deploy checks clean. No migration
  was required. The development Celery worker was restarted, registered
  `classification.classify_report`, and reached ready state. Next: T4.6 Issue severity = max of
  member Report severity.
- **T4.6 implementation record (2026-08-14):** complete. Every successful Report attachment now
  recomputes the Issue's stored severity from the full persisted member set inside the same locked
  clustering transaction. `SEVERITY_RANK` supplies only the fixed
  `{Critical, High, Medium, Low}` ordering: corroboration and proximity remain excluded. The oldest
  highest-severity Report (UUID as the final tie-breaker) deterministically supplies the
  human-readable classification rationale, preventing equal-band retries from churning the
  explanation. Computed severity continues to update beneath an authority override without changing
  the override, its reason, or the displayed `current_severity` (BR-20/21). Tests cover severity
  increases, lower members, full-set repair of stale computed data, stable equal-band rationale,
  override preservation, the classification-to-clustering worker path, and two concurrent
  mixed-severity Reports converging on one correctly ranked Issue. Validation: 1012 passed with no
  xfails; ruff, format and strict mypy clean; model drift and the production deploy check are clean.
  No migration was required. The development Celery worker was restarted, registered
  `classification.classify_report`, and reached ready state. Next: T4.7 one Confirmation per citizen
  per Issue and derived corroboration count.
- **T4.7 implementation record (2026-08-14):** complete. `Confirmation` is a revocable,
  UUID-addressed relationship from one Citizen to one Issue, protected from hard deletion and
  guarded by the database-level `issues_confirmation_one_per_citizen` uniqueness constraint. The
  service locks the target Issue so concurrent repeats produce exactly one row and one
  `409 ALREADY_CONFIRMED`; moderated Issues remain non-disclosing `404`s. API §6.6's nested
  `POST /issues/{id}/confirmations` and `DELETE /issues/{id}/confirmations/me` are live,
  session-authenticated, Citizen-only and CSRF-protected. `corroboration_count` is a read-only
  derivation over distinct active Report authors plus confirmers, so repeated Reports and a
  Report-plus-Confirmation from the same account count once, inactive accounts count zero, and
  withdrawal can decrease the result. Verification/age weighting remains deliberately absent:
  those inputs exist, but the product specifies no threshold or weight, and inventing one would
  recreate the numeric scoring FR-21 removed. Corroboration remains display-only and never enters
  severity. Validation: 1029 passed with no xfails; ruff, format, strict mypy, model drift and the
  production deploy check clean. Migration `issues.0004_confirmation` passed
  forward/reverse/forward on a fresh PostGIS database and is applied to development. Next: T4.8
  display-only proximity context.
- **T4.8 implementation record (2026-08-14):** complete. `geo.POI` now holds Admin-managed,
  retire-only reference points with the controlled hospital/school/highway/market vocabulary, a
  WGS84 geography location, a named GiST spatial index, source provenance and active state. The
  reusable `nearby_pois()` read selector accepts an explicit metre radius and result limit, filters
  inactive rows, applies index-assisted `ST_DWithin`, and orders nearest-first with a deterministic
  UUID tie-break. No POI seed was added because the authoritative source/licence remains an open
  data decision. There is deliberately no Issue field, Issue-to-POI relationship or stored
  proximity value: the selector is the response-building primitive for the later Issue list/detail
  surfaces, which do not exist yet, and POIs cannot influence severity, queue ordering, clustering
  or any business rule (C-10). Validation: 1039 passed with no xfails; ruff, format, strict mypy,
  model drift and the production deploy check clean. Migration `geo.0003_poi` passed
  forward/reverse/forward on a fresh PostGIS database and is applied to development. Next: T5.1
  Issue status enum and transition table.

**M4 gate:** two concurrent same-category nearby reports → exactly one Issue; severity = max of
members; corroboration count is derived and read-only; proximity context is display-only.

## C6. P5 Issue Triage Workflow
- T5.1: the status state machine is validated in the service layer. No FSM library — the transition
  set is small and has custom reason-required rules. Illegal transitions → `409 INVALID_TRANSITION`.
- T5.3: every status transition writes an immutable Status Event in the same transaction. No
  separate call, no async — it is part of the atomic state change.
- T5.5: severity override retains *both* the computed severity and the override. Neither overwrites
  the other. The override requires a mandatory reason (`422` if absent).
- T5.6/T5.7: merge/split re-attributes all Reports and Confirmations to the surviving Issue;
  severity is recomputed as max. Both operations require a reason. Split requires each side to keep
  ≥1 report.

- **T5.1 implementation record (2026-08-14):** complete. The Issues service now owns the
  authoritative lifecycle transition table: `submitted → triaged → acknowledged → in_progress →
  resolved → closed`, with the documented `rejected`, `duplicate`, and `insufficient_info` branches
  from `triaged`. Same-state writes, skipped edges, terminal returns, moderation-state changes and
  unknown statuses fail as `409 INVALID_TRANSITION`. Branches and `reopen` require a non-blank,
  trimmed reason and fail as `422` when absent. Reopen is represented as an explicit validated
  create-new-Issue plan only from `resolved`/`closed`; it is not a persisted status and never
  reactivates the historical Issue (DM-Q8). T5.2 will consume this plan for the atomic PATCH
  mutation and linked-Issue creation, while T5.3 adds the immutable Status Event. Validation: 1095
  passed with no xfails; ruff, format, strict mypy, model drift and the production deploy check
  clean. No migration was required. Next: T5.2 status mutation endpoint.

**M5 gate:** full lifecycle works; illegal transitions rejected; every transition writes an immutable
event; override preserves computed value; merge/split re-attributes correctly.

## C7. P6 Notifications & Outbox
- T6.1: the outbox table is written in the **same transaction** as the state change. Not after, not
  in a `on_commit` hook — in the same `atomic()` block. The beat relay polls it with `SKIP LOCKED`.
- T6.2: the dispatcher is at-least-once. Consumers must be idempotent (check for already-delivered
  before sending).
- T6.6: SMS is gated to **High severity, server-side**. A user preference to enable SMS does not
  bypass this gate. The gate is not configurable per user (R-8).
- T6.8: SSE requires the ASGI stack. Confirm `uvicorn` is running (not `gunicorn` in sync mode)
  before testing SSE.

**M6 gate:** status change → notification dispatched via outbox; SMS only fires for High severity;
SSE stream delivers in-app notifications; outbox survives a worker crash (at-least-once).

## C8. P7–P10 (brief)
- **P7 (Read paths):** severity-ranked queue uses `?sort=severity` (default DESC then age). Map
  endpoint returns GeoJSON `FeatureCollection` via `GeoFeatureModelSerializer`. Analytics are
  read-only aggregations from `selectors.py`.
- **P8 (Moderation + Audit):** audit log is append-only, enforced at the DB role level (revoke
  `UPDATE`/`DELETE` from the app role — T8.1). Test the revoke with an integration test that expects
  the write to fail. Reference-data admin (taxonomy, keywords, clustering rules) is Django admin.
- **P9 (Export):** export jobs are async (Celery). Export links are short-lived signed URLs
  (`expiresAt`). CSV and GeoJSON formats.
- **P10 (Hardening):** `manage.py check --deploy` must be clean. `DEBUG=False` everywhere deployed.
  `/metrics` not on the Ingress. Load test against NFR latency/throughput targets. Security review
  against OWASP Top 10. Rotate `DJANGO_SECRET_KEY` on a schedule.

---

# Part D — Hard prohibitions (quick reference)

These are non-negotiable. Each has a doc citation; if you think one should change, update the cited
doc first.

| Do not | Why |
| --- | --- |
| Use JWT | Sessions required for immediate revocation (Arch §8) |
| Add `POST /issues` | Issues form via async clustering only (API §9) |
| Write to status-events or audit-events | Append-only, enforced at DB level (C-9, BR-31, T8.1) |
| Hard-delete users, categories, POIs, Issues | Retire / anonymize (C-14, FR-31) |
| Let POI/proximity data affect severity or ordering | Display-only (C-10) |
| Add outbound webhooks / government integration | PRD §2.2 non-goal |
| Add a numeric priority score or tunable weights | FR-21 explicitly removed |
| Use `contrib.auth` Groups/Permissions for RBAC | Cannot express BR-26 category scoping |
| Run `migrate` in the Dockerfile or entrypoint | DevOps §2.2 |
| Deploy `latest` | Always a SHA-tagged image (DevOps §2.3) |
| Expose `/metrics` publicly or on the Ingress | DevOps §8.2/§9 |
| Set `readOnlyRootFilesystem: true` without `/tmp` emptyDir | Breaks uploads (DevOps §9) |
| Leave `DEBUG=True` in any deployed environment | DevOps §8.1 |
| Commit secrets | `.env.local` ignored; `.env.example` has placeholders only |
| Add frontend code | Plan + DevOps scope: backend only |
| Let code diverge from `04-api-specification.md` | Amend the spec first (DC-3, R-9) |
| Invent answers to open questions Q1/Q3/Q5/Q6/Q10 | Flag them as decision gates |

---

# Part E — Commands reference

All sourced from `docs/06-devops-guide.md` §4.1 and its Dockerfile. No task runner exists yet.

```bash
# Lint + type-check
ruff check .
ruff format --check .
mypy .

# Migration checks
python manage.py makemigrations --check --dry-run   # model drift gate
python manage.py check --deploy                     # security config gate

# Apply + verify migrations
python manage.py migrate
python manage.py migrate <app> zero                 # reversibility check

# Tests
pytest                                              # all tests
pytest tests/unit/                                  # unit only (no external deps)
pytest tests/integration/                           # integration (needs DB + Redis)

# Static assets (build-time only)
python manage.py collectstatic --noinput

# Run processes
uvicorn urbenmend.asgi:application --host 0.0.0.0 --port 8080   # API
celery -A urbenmend worker -B --loglevel=info                   # worker + beat (local only)

# Docker
docker build .
docker compose up
docker compose run --rm api python manage.py migrate
```

---

# Part F — CI pipeline (all 7 stages must pass before merge)

Sourced from `docs/06-devops-guide.md` §4.1.

| Stage | Command | Fails on |
| --- | --- | --- |
| 1. Lint & type-check | `ruff check` + `ruff format --check` + `mypy` | Any lint or type error |
| 2. Model drift | `manage.py makemigrations --check --dry-run` | Model changed without migration |
| 3. Deploy check | `manage.py check --deploy` (prod settings) | Any Django security warning |
| 4. Unit tests | `pytest` (no external deps) | Any test failure |
| 5. Integration tests | `pytest` (real PostGIS + Redis) | Any test failure or migration error |
| 6. Build + scan | `docker build` + vulnerability scan (e.g. Trivy) | Build error; critical/high CVE |
| 7. Push image | Push SHA-tagged image | Registry auth failure |

Integration tests need `postgis/postgis` as a CI service and a DB user with `CREATE EXTENSION`
permission. The CI DB user must differ from the app runtime role (the app role has `UPDATE`/`DELETE`
revoked on audit tables — T8.1).

---

# Part G — Commit message format

```
<type>(<task-id>): <short description> (<requirement-ids>)

Examples:
feat(T2.2): async report submission returning 202 (FR-5, NFR-3)
fix(T1.3): session revocation on account suspension (BR-25)
refactor(T0.6): custom error envelope handler (API §4.1)
test(T4.4): concurrent clustering creates exactly one issue (R-2)
docs(T0.5): add CI pipeline stage for model drift check
```

Types: `feat` `fix` `refactor` `test` `docs` `chore`. Every commit cites the task ID. Every
feature commit cites at least one requirement ID.

---

# Part H — Definition of Done (project-wide)

From [05-project-plan.md](05-project-plan.md) §11.

**Functional:**
- [ ] All approved FRs implemented and traceable to tasks
- [ ] All open questions resolved or explicitly deferred with rationale
- [ ] Report → Classify → Cluster → Triage → Notify pipeline works end-to-end
- [ ] Report/Issue separation preserved (severity/status/assignment only on Issue)
- [ ] No numeric scoring; corroboration/proximity display-only
- [ ] RBAC + authority category-scoping enforced everywhere; audit log complete

**Quality:**
- [ ] NFR latency/throughput/geospatial targets met under load
- [ ] Reliability primitives (outbox, fallback, retries) verified via failure drills
- [ ] Security and privacy reviews signed off
- [ ] Unit + integration + contract + concurrency + regression tests all green in CI

**Delivery:**
- [ ] API implementation matches `04-api-specification.md`; deviations reconciled in the spec
- [ ] Deployment, runbook, and observability docs complete (DC-6)
- [ ] Open-question resolution log complete (DC-7)
- [ ] Demo scenario rehearsed for capstone defence
- [ ] Handover package: source, migrations, env setup, planning docs 01–07
