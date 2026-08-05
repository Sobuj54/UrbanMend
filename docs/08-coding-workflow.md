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

The three `_seed_*` helpers stand in for the `factory_boy` factories T2.1/T4.2 will own (they
cannot be written before the models). When the models land, the diff is confined to those
helpers; the assertions stand unchanged.

⚠️ **The test has never actually exercised a lock** — it XFAILs at the first import. It pins the
contract, not the implementation. T4.4 is where it first runs for real, and a green run there is
only meaningful if the marker is removed in the same commit.

## M0 gate — do not start P1 until all of these hold

- [ ] CI is green on all stages
- [ ] API and Worker both boot
- [ ] Migrations apply cleanly **from zero**
- [ ] `/health` reports each dependency's state
- [ ] A trivial round-trip request returns the standard error envelope on failure
- [ ] Python/Django/DRF versions pinned; settings-module naming conflict resolved
- [ ] DC-1 written: environment/setup notes + runbook skeleton + migration guide

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

**M1 gate:** a citizen can register → verify → log in; an admin can provision a scope-limited
authority; RBAC denies out-of-scope actions; sessions revoke immediately.

## C3. P2 Reporting & Media
- T2.2: the `POST /reports` response is `202 Accepted` — the report is written synchronously, triage
  is async. The response body is the created report resource (not empty). Return immediately; do not
  wait for classification.
- T2.3: idempotency key is checked *before* the write, in the service. A replay returns the original
  `201` result. Key is scoped per user, stored in Redis.
- T2.5: EXIF orientation must be applied *before* stripping — Pillow's `ImageOps.exif_transpose`
  then strip all EXIF. Never store the original EXIF-bearing file.
- T2.1: boundary check uses PostGIS point-in-polygon (`ST_Within` or `dwithin` against the city
  polygon). Reject with `422 OUT_OF_CITY` if outside.

**M2 gate:** report + photo submitted → `202` immediately; photo stored with EXIF stripped; citizen
can retrieve own reports; out-of-city and invalid submissions rejected correctly; duplicate submits
are idempotent.

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

## C5. P4 Clustering & Issues
- T4.4 is the hardest task in the project. The concurrency-safe find-or-create must use a
  **Postgres transaction-scoped advisory lock** keyed on `(geohash_cell, category_id)` inside
  `atomic()`. Test it with two concurrent requests — assert exactly one Issue is created.
- T4.6: Issue severity = max of member report severity signals. Recompute on every new member.
  Enum is `{Critical, High, Medium, Low}` — four bands (Q2 resolved). The stale three-band
  references in `03-data-model.md` §3 are superseded.
- T4.8: proximity context (POIs near the issue) is **display-only**. It must never influence
  severity, ordering, or any business rule (C-10). Compute it for the response; do not store it as
  a field that could be misread as authoritative.

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

