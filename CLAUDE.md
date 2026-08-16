# UrbanMend — Claude Code Project Memory

Civic issue-reporting backend for a single city (initial deployment: Bangladesh). Citizens submit
geolocated photo reports; a hosted-LLM triage layer assigns category and severity
(Critical/High/Medium/Low); nearby reports cluster into Issues that Authorities act on. Actors:
**Citizen**, **Authority**, **Admin**. **Backend only** (API + Worker).

**Report ≠ Issue** (PRD §6.1): a **Report** is one citizen submission; an **Issue** is a cluster of one
or more Reports = one real-world problem. Severity, status and assignment live on the **Issue**, never
on the Report. **Naming:** the product is "UrbanMend", the package/image identifier is `urbenmend`
(`urbenmend.asgi:application`, `celery -A urbenmend`).

## Stack (committed — 02-architecture.md §2.3, ADR-001 Accepted)

Python + Django + DRF · PostgreSQL + PostGIS · GeoDjango + `djangorestframework-gis` · Django
migrations · ASGI (uvicorn) · Celery (Redis broker) + beat · Redis for cache/rate-limit/idempotency ·
S3-compatible storage via `django-storages` · `ruff` + `mypy` · `pytest-django` + `factory_boy`.

- ✅ T0.1: **Python 3.13** (a **ceiling**, not a preference — `djangorestframework-gis` 1.2.1 caps
  there), **Django 5.2.16 LTS**, **DRF 3.17.1**, `python:3.13-slim`; deps via **pip-compile**
  (`requirements/{base,dev}.in` → `.txt`, `--generate-hashes`; `dev.txt` needs `--allow-unsafe`).
- ✅ A9/T0.5: **CI vendor is GitHub Actions** (user's choice; no doc named one). The seven-stage order
  is doc-mandated (DevOps §4.1) and vendor-independent.
- **Unpinned: cloud host, LLM provider (❓Q9). Do not invent these — raise them.**

## Context: planning docs

`@docs/08-coding-workflow.md` — **the build records in §C1–C2 carry the full reasoning behind every ⚠️
line below. Read the relevant record before changing anything marked ⚠️.** This file is the index; that
file is the argument. The rest are large; **read on demand**: `01-prd.md` (FR-/NFR-/BR- IDs, non-goals,
edge cases) · `02-architecture.md` (module boundaries, layering, sessions, outbox) · `03-data-model.md`
(domain entities — **no schema/SQL**) · `04-api-specification.md` (endpoint contracts —
**authoritative over the implementation**) · `05-project-plan.md` (phases, task IDs) ·
`06-devops-guide.md` (containers, CI, migrations, K8s, observability) · `07-adr-001-app-framework.md`
(why Django/DRF) · `09-operations.md` (**DC-1** — local setup, env vars, runbook, migration guide).

## Repo structure

```
manage.py  pyproject.toml  Dockerfile  docker-compose.yml  requirements/
urbenmend/
  settings/{base,dev,prod,build}.py  urls.py  asgi.py  wsgi.py
  api/  identity/  reporting/  media/  classification/  issues/  geo/
  notifications/  moderation/  audit/  export/  platform/
```

- ⚠️ Apps **nest under `urbenmend/`** and import as `urbenmend.<label>` — a root-level `platform`
  package shadows the stdlib module Django imports (app *labels* are unchanged). Each app carries
  `apps.py`, `models.py`, **`services.py` (writes + authorization)**, **`selectors.py` (reads)**,
  `admin.py`, `migrations/`, `tests/` from day one; DRF views stay thin.
  `urbenmend/platform/tests/test_app_skeleton.py` enforces this — add the same file set or it fails.
- Settings split is **`base`/`dev`/`prod`** (+ `build`); DevOps §3.2's `settings.local` was amended to
  `settings.dev` — **do not reintroduce it**. ⚠️ **`DJANGO_SECRET_KEY` and `DATABASE_URL` are required
  with no fallback** in `base`/`prod`: `build.py` injects throwaways so build-time `collectstatic`
  needs no secrets, `dev.py` has local-only fallbacks. **Never add a fallback to `prod.py`.**

## Build state

**Done:** A1/A4–A7 (settings split, skeleton, `AUTH_USER_MODEL`, baseline migration) · T0.6 (camelCase
layer + `{data, page, meta}` envelope + error handler) · T0.10 (`Category`, seven-node taxonomy, ✅ ❓Q1
resolved) · T1.2–T1.9 (registration/verification, sessions, CSRF, RBAC, provisioning, 2FA, throttling,
`/users/me`) · T2.1–T2.9 (report/media intake, processing, reads, edits, idempotency, throttling) ·
T3.1–T3.7 (LLM abstraction, keyword fallback, controls, async classification) · T4.1–T4.8 (Issue,
clustering, severity, confirmations, proximity) · T5.1–T5.6 (workflow, status events, assignment,
severity override, merge).

Suite after T5.6 (2026-08-16): **1150 passed**, mypy 170 source files and ruff clean, no migration
drift. **Next: T5.7** (split issues).

**Unowned / blocked — do not silently absorb into another task:** `/auth/password/forgot`·`/reset`
(blocked on ❓Q5; **a provisioned Authority still has no way to set a first password**, so T1.6 accounts
cannot log in) · `GET /users` and `PATCH /users/{id}` (Admin, API §6.2 — need a task ID; that `PATCH` is
the documented route for Authority deprovisioning *and* Admin-side email changes, so **two T1.9
decisions have no implemented alternative path**).

## ⚠️ Traps that bind future work

Each is a rule someone would plausibly "simplify" away; the matching record in
`docs/08-coding-workflow.md` says what breaks.

### Migrations & data

- ⚠️ **`CreateExtension("postgis")` stays the first operation of `identity/0001`** (the earliest
  project-owned node); a geometry app must name it in `dependencies`. **`identity/0001` is frozen.**
- ⚠️ **`postgis/postgis` pre-creates the extension, so the compose DB proves nothing** — verify on a
  fresh `CREATE DATABASE`. "No migrations to apply" is not reversibility; `[X]` → `[ ]` → `[X]` is.
- ⚠️ **`RunPython` needs a real reverse deleting only what it seeded**, never `Model.objects.all()`.
  Taxonomy/boundary are seeded data (NFR-11): `apps.get_model()`, GEOS imported *inside* the function.
- **Nullable → backfill → tighten**, never `AddField(unique=True)` against populated rows.

### Identity & auth

- ⚠️ **`email`/`phone` are nullable + UNIQUE; absence is `NULL`, never `""`.** The
  `identity_user_has_contact_or_anonymized` constraint's `status=deleted` branch is what makes C-14
  anonymization possible — **don't tighten it.**
- **`is_active` is a derived read-only property** (move `status`) · verification is **timestamps**, not
  booleans · **normalization lives in `save()`** (DRF skips `full_clean()`) · ⚠️ **`PermissionsMixin` is
  admin plumbing only** — RBAC is `role` + scope in `services.py`, never `groups`/`user_permissions`.
- ⚠️ **`verify_code()` must never be `@transaction.atomic`** — the attempt increment must commit before
  the comparison that may reject it, or `MAX_ATTEMPTS` never fires. Two blocks, `select_for_update()`.
  **Codes are Argon2-hashed**, plaintext returned as a tuple element; **failure raises, never returns
  `False`** (a bool invites `if verify_code(...)` with no `else` — fails open). **Pre-session
  `/auth/verify` gives one generic message for every failure.**
- ⚠️ **Revoke via `SessionStore(session_key=...).delete()`, never `Session.objects.filter().delete()`**
  — on `cached_db` the cached copy keeps authenticating (BR-25, BR-33). **`start_session()` wraps
  `django.contrib.auth.login()`** for `cycle_key()`, `backend=` explicit; **`start_partial_session()`
  calls `cycle_key()` itself.**
- ⚠️ **Password is checked BEFORE status**; `AccountLockedError` subclasses `AuthenticationError` and
  the view's `except` clauses are **locked-first** — reversed, every `403 ACCOUNT_LOCKED` becomes `401`.
  **An unknown identifier still hashes a throwaway password** (timing oracle). **`LoginSerializer`
  validates shape only** — no `EmailField`, no `min_length`, `trim_whitespace=False`.
- ⚠️ **DRF's `NotAuthenticated` → `403` rewrite is undone once, globally** (§4.2 requires `401`); no
  per-view `handle_exception`, and login failures use a plain `APIException`. ⚠️ **CSRF is enforced only
  on authenticated requests** — any view with `authentication_classes = []` drops it silently;
  `identity/tests/test_csrf.py` is the catch.

### Two-factor (T1.7)

- ⚠️ **The partial session works by NOT calling `django_login()`** — `SESSION_KEY` stays unset, so every
  authenticated endpoint `401`s automatically. **django-otp's `otp_required` was rejected**: it logs the
  user in fully then gates views one at a time, so a view added later fails open. ⚠️ **The two 2FA
  routes are the only ones accepting a non-authenticated session.**
- ⚠️ **`device.key` is hex, `config_url` is base32** — returning `device.key` as `secret` makes QR work
  and manual entry silently wrong forever. ⚠️ **Don't reimplement `verify_token()`**: it stores
  `last_t`, the only replay protection inside a code's own 30-second window.
- **A confirmed device is checked before an unconfirmed one**; a flagged account with **no** device
  reads `requires_two_factor() == True`, escaped by `/auth/2fa/enroll` accepting a partial session (an
  unconfirmed device does not opt an account in). **`resolve_partial_session_user()` re-fetches and
  re-checks `is_active`.** **The `user` object is omitted while `requires2fa` is true** — `null` leaks
  the same distinction by shape.

### RBAC & provisioning (T1.5/T1.6)

- ⚠️ **An empty `category_scope` grants nothing** — never read empty as "unrestricted". **Admins bypass
  scope**: `scoped_category_ids()` returns `None` = no filter (a row-per-category Admin loses access the
  moment a migration adds a node). ⚠️ **`has_role()` checks `status` too** — a suspended Authority still
  reads `role == "authority"`.
- ⚠️ **`403` to act, `404` to see** — a `403` on a scoped read confirms the id exists elsewhere.
  **Denial messages name neither role nor resource.** ⚠️ **Test scope with `.filter(pk=...).exists()`,
  never `category in scope.all()`** (which caches). **`AuthorizationError` subclasses Django's
  `PermissionDenied`, not DRF's.**
- ⚠️ **BR-25's audit is a structured log line, not a record — knowingly.** The immutable table is
  **T8.1** (append-only via revoked `UPDATE`/`DELETE`). Everything funnels through
  `_audit_privileged_action()`; **T8.1 replaces its body — never add a second call path beside it.**
  (Admin's scope editor bypasses it: break-glass for FR-30/31.)
- **New authorities start `registered`** (BR-30) · **retired categories are rejected `422`, never
  dropped** · **this `409` is specific, registration's is generic** — don't unify · **`role`/`status` in
  the body are ignored.** ⚠️ **`set_category_scope()` replaces, never merges** (`[]` legitimately
  revokes all), and **checks the `role` column, not `has_role()`**, or a suspended Authority is
  reinstatable only with the wrong scope.
- ⚠️ **No `IsAdminUser`** — DRF's checks `is_staff`, not the domain `role`; `require_role()` in the
  service is the enforcement point (FR-3). ⚠️ **`users/authorities` and `users/me` must stay routed
  before any `users/<id>` pattern.**

### Categories (T0.10)

- ⚠️ **`Other / Uncategorized` is a required sink** — off-taxonomy LLM output coerces to it, so deleting
  or retiring it breaks the fallback. **`slug` is the machine key** (`roads`, `water_drainage`,
  `electrical` are quoted in §6.2 and are therefore contract); **bilingual labels are non-null**
  (NFR-8); **lifecycle is `Active → Retired`, never deleted.**

### Rate limiting (T1.8)

- ⚠️ **Lockout is throttle-only backoff — no per-account lock state** (persistent lockout is a targeted
  DoS). **`403 ACCOUNT_LOCKED` stays a `status` denial**; never reuse it, never reuse `SUSPENDED`.
- ⚠️ **DRF's `parse_rate` reads only `period[0]` — `"5/15m"` silently means 5 per *minute*.**
  `ScopedWindowRateThrottle` overrides it. ⚠️ **Rates are read at instantiation, not bound as a class
  attribute**, or `override_settings` cannot reach them. **`DEFAULT_THROTTLE_CLASSES` stays unset.**
- ⚠️ **DRF emits no `RateLimit-*` headers** (§4.5 requires three) — `RateLimitHeadersMixin` overrides
  **`get_throttles()`**; invented `request.throttle_*` attributes emit nothing, silently. The advertised
  bucket is the one with least **headroom**.
- ⚠️ **`AuthIdentityRateThrottle` keys on the SHA-256 of the normalized identifier, never raw**
  (NFR-12); parse failures return `None`, never raise. ⚠️ **`clear_identity_throttle()` is success-path
  only** — in a `finally` it removes the counter and every happy-path test still passes. **Throttle runs
  before the credential check**, and **2FA guessing lands in `auth_anon`** — that is the bucket to
  tighten for OTP. ⚠️ **Throttle and idempotency state live in Redis and are NOT rolled back between
  tests** — the root `conftest.py` clears the cache autouse around every test.

### Profile & deletion (T1.9)

- ⚠️ **An Admin's `categoryScope: []` and an unscoped Authority's `[]` are identical JSON with opposite
  meanings**; `role` is the only disambiguator. ⚠️ **`DELETE /users/me` is Citizen-only** — the
  alternative, `PATCH /users/{id} {"status":"deprovisioned"}`, is **unbuilt (above)**.
- ⚠️ **Anonymization nulls PII in the *same* `save()` as the status flip** — two UPDATEs leave a crash
  window with a `deleted` row carrying a live email. **The row is retained, never deleted** (C-14;
  `test_anonymization_retains_the_row`). **`VerificationCode`/`TOTPDevice` deletes are explicit** (no
  cascade fires) and **sessions are revoked inside the transaction.**
- ⚠️ **`PATCH /users/me` rejects unknown fields rather than dropping them** (DRF's default makes
  `{"role":"admin"}` a `200`); allowed keys derived in **both spellings**. ⚠️ **`ProfileUpdateSerializer`
  is a plain `Serializer`, never a `ModelSerializer` over `User`**, and **`email` is not updatable
  here** — the test asserts the *signature*.
- ⚠️ **Any submitted `phone` clears `phone_verified_at`, even unchanged** (BR-30) · **`""` clears it,
  `null` is refused** · **clearing the last channel is `422`** · **the `409` uses `.exclude(pk=user.pk)`.**

### Reporting & geo (T2.1)

- ⚠️ **Intake fails closed with no boundary**: `active_city_boundary()` **raises rather than returning
  `None`**, so nobody can write `if boundary and not contains(...)` — which accepts every location on
  Earth once the table empties. Raises on zero *and* two active rows; replacement is **add-and-retire**.
- ⚠️ **`GistIndex`, never `models.Index`, on `location`** (with `spatial_index=False`) — a B-tree cannot
  serve `ST_Within`/`ST_DWithin`, so spatial queries become sequential scans while the migration reads
  as indexed. BR-35, T4.4 clustering and the T7.4 map bbox rest on it.
- ⚠️ **`OutOfCity` (`422`) is its own type, not a `ReportValidationError` (`400`)** — collapsed, a
  client cannot tell "fix your JSON" from "we do not serve your city". ⚠️ **Order is authorization →
  location → content, and it is observable.**
- ⚠️ **`ReportStatus` holds no Issue-workflow value, asserted by name** — adding one gives two rows one
  answer to "is this fixed?". ⚠️ **`SeveritySignal` lives in `reporting`, shared with `issues`; four
  bands**; `SEVERITY_RANK` exists **only** so BR-11's "highest" is computable — not a score (FR-21), not
  tunable, not exposed, never combined with corroboration or proximity (C-10).
- ⚠️ **`is_classified` keys on `classified_at`, not `category`** — a citizen *hint* fills `category`, so
  keying on it makes T3.5's worker skip the report forever. **A hint is not a classification**: an
  unknown *or retired* slug is refused **`400`**, never coerced to `Other` (BR-7 is for LLM output).
- ⚠️ **`# noqa: DJ001` on `severity_signal`/`classification_source` is deliberate** — `""` is an
  undeclared fifth band that validates only because Django skips blanks, and `SEVERITY_RANK[""]` raises
  inside T4.6's `max()`. (DJ001 exempts `unique=True`, so it never fired on `email`/`phone`.)
- ⚠️ **`author` is `PROTECT`** (a hard delete must fail loudly, not erase FR-16's corroboration count) ·
  ⚠️ **the Issue FK is absent until T4.1**, asserted, so whoever adds it meets BR-6 · **unverified
  citizens may submit** (BR-30 gates notification, not intake) · ⚠️ **`create_report()` takes no
  `status` parameter and the test asserts the *signature*.**
- ⚠️ **`ReportFactory` bypasses `create_report()` and builds *unclassified*** (fixtures routed through
  the service make one validation bug fail unrelated suites); **`CityBoundaryFactory` defaults
  `is_active = False`**, unlike the model. ⚠️ **Use `F.create()`/`F.build()`, never `F()`** —
  `FactoryMetaClass.__call__` is unannotated, so `UserFactory()` types as the *factory*.

### `POST /reports` (T2.2)

- ⚠️ **`transaction.on_commit` is the task, asserted from the failing side** — an inline `.delay()` lets
  an idle worker `SELECT` the report pre-commit, read nothing, and never triage it (load-dependent, so
  it survives every local run). ⚠️ **Never configure `task_always_eager`**: eager runs the body inside
  the caller's uncommitted transaction, so **the suite passes with the broken inline `.delay()`.** Patch
  `.delay` at the **use** site; drive callbacks with `captureOnCommitCallbacks(execute=True)`.
- ⚠️ **Only the id crosses the broker, as a `str`, bound to a local before the closure** — capturing
  `report` puts PII in Redis (NFR-12) and lets the worker act on a pre-commit snapshot. ⚠️ **`PROCESSING`
  is a second UPDATE in the same transaction**, and `update_fields` **must list `updated_at`**.
- ⚠️ **The Celery task has an explicit `name=`** — otherwise it follows the module path and moving the
  module orphans queued messages (`NotRegistered`, post-deploy); `test_tasks.py` asserts it because mypy
  cannot. **The stub logs and returns `None`** (T3.5 fills it, ❓Q9).
- ⚠️ **`LocationSerializer.to_point` is a `staticmethod` over a mapping** — a nested serializer has no
  `validated_data`. The `(lng, lat)` order lives in one place; transposed, every real submission reads
  as out-of-city. Degree bounds are why `lat: 200` is `400`, not a misleading `422`.
- ⚠️ **`mediaIds` is declared and refused (`400`), never dropped** — DRF discards unknown keys, so a
  photo-only submission would fail BR-3 complaining about `description`. **`ReportSubmitSerializer` is a
  plain `Serializer`** · **the response reads `status` off the row** · **`202`, no `Location`** · **no
  role class on the view, no local `except`.**
- ⚠️ **The camelCase layer wraps `run_validation`, not `to_internal_value`** — the narrower method
  leaves object-level errors untranslated, a half-fix that passes every field-level test. **Leaves stay
  `ErrorDetail`**; as `str` they lose `.code` and every envelope `issue` degrades to `INVALID`.

### Idempotency (T2.3)

- ⚠️ **`cache.add()`, never `get()` then `set()` — that one line is the entire concurrency claim.**
  `add()` is `SETNX`; a read-then-write pair reads as equivalent and lets *both* callers through. R-2's
  test fires 8 threads through a `threading.Barrier` — **the barrier is what makes it a race** — against
  real Redis, no ORM (locmem asserts a guarantee the deployed path does not get).
- ⚠️ **`complete()` runs from `transaction.on_commit`, never inline** — a completed record promises the
  row exists, so written pre-commit a rollback leaves it replaying a `reportId` that never persisted.
  The consequence (a concurrent double-tap gets `409 IDEMPOTENCY_IN_PROGRESS`) **is the guarantee.**
- ⚠️ **A failed request does not consume its key** — `release()` from a broad `except Exception`; the
  client's next move is a corrected body, i.e. a *different* fingerprint. ⚠️ **`submit_report()` returns
  `SubmissionAcknowledgement`, not `Report`** — what makes §6.3's "verbatim" true by construction;
  `issueId`/`classification.state`/`status` live in `_acknowledge()`, evaluated **at acceptance time**.
- ⚠️ **The fingerprint is checked before the state, and both states agree.** **Comparison is on
  normalized values, not raw bytes**, or a reordered-JSON retry `409`s. ⚠️ **Nothing identifying reaches
  Redis**: `idempotency:<scope>:<sha256(scope|user|key)[:40]>` — the key is a bearer token for a stored
  response; scope stays readable so ops can `SCAN`.
- ⚠️ **Two TTLs, and the shorter is not tuning** — 60s is the only backstop for a process killed
  mid-write; at retention length one crash locks a key for a day. ⚠️ **A vanished or unreadable record
  reads as a fresh claim** — via `set()`, not a bare pass, or a concurrent duplicate finds it empty too.
- ⚠️ **Authorization runs before the key is examined** · **blank key = no de-duplication**, `""` must
  never become a key · **body-inferred keys were rejected** (two identical reports are two corroborating
  voices) · **over-long keys raise, never truncate** · ⚠️ **`IDEMPOTENCY_SCOPE` is a stored cache-key
  component** · **`Idempotency-Replayed` only on a replay**, `replayed` undeclared on the serializer ·
  **no idempotency table.**

### Numbers we chose (not spec-derived — `api-conventions.md` lists these under "do not invent")

All in `settings/base.py`, env-overridable (NFR-11), reason inline: verification `CODE_LENGTH=6` /
`TTL=10min` / `MAX_ATTEMPTS=5`; `AUTH_THROTTLE_RATES` (`auth_anon` 10/15m per IP, `auth_identity` 5/15m
per identifier, `auth_user` 20/15m per session); `IDEMPOTENCY_RETENTION_SECONDS` 86 400 /
`_IN_PROGRESS_SECONDS` 60 / `_KEY_MAX_LENGTH` 255. **A cache flush drops every held idempotency key and
every throttle counter** — `default` is one Redis.

## Commands

All gates run **inside the api container**: `docker compose exec -T api sh -c "…"`.

```bash
ruff check && ruff format --check          # lint
mypy                                       # type-check
pytest                                     # unit + integration
python manage.py makemigrations --check --dry-run   # model drift
python manage.py check --deploy            # security config (needs a long SECRET_KEY, or W009 fires)
python manage.py migrate                   # apply migrations
python manage.py collectstatic --noinput   # build-time only
uvicorn urbenmend.asgi:application --host 0.0.0.0 --port 8080   # api
celery -A urbenmend worker -B --loglevel=info                   # worker + beat
docker build .                             # image
```

## Conventions

Detail in `.claude/rules/`, which loads by file pattern: `api-conventions.md`, `auth.md`,
`database.md`, `async-worker.md`, `testing.md`.

- URI versioning `/api/v1`; plural lowercase nouns; `camelCase` JSON; ISO-8601 UTC; opaque IDs in URLs,
  never sequential or guessable
- Collections return `{ data, page, meta }` with **cursor** pagination (mandatory, limit 20/max 100);
  single resources return the bare object. Errors: `{ error: { code, message, details, traceId } }`
- Auth is **server-validated sessions** in a `Secure`/`HttpOnly`/`SameSite` cookie + CSRF — not JWT
- Authorization is enforced in the **service layer**, on every mutating and sensitive-read action —
  call the T1.5 primitives in `identity/services.py`; do not reimplement a role or scope check

## Do not

- **Do not use JWT** — sessions are required for immediate revocation (Arch §8). **Do not use
  `django.contrib.auth` Groups/Permissions for RBAC** — cannot express BR-26 scoping.
- **Do not add `POST /issues`** (Issues form only via async clustering), **write endpoints for status-
  or audit-events** (append-only, C-9/BR-31), **outbound webhooks or government-system integration**
  (PRD §2.2 non-goal), or **frontend code** (backend-only repo).
- **Do not hard-delete** users, categories, POIs or Issues — retire; user deletion anonymizes (C-14).
- **Do not add a numeric priority score or tunable weights** (removed, FR-21), and **do not let
  POI/proximity data affect severity or ordering** — display-only (C-10).
- **Do not run `migrate`** in the Dockerfile or entrypoint. **Do not deploy `latest`** — SHA-tagged
  only. **Do not expose `/metrics`** publicly. **Do not set `readOnlyRootFilesystem: true`** without an
  `emptyDir` at `/tmp`. **Do not leave `DEBUG` enabled** anywhere deployed.
- **Do not commit secrets** — `.env.local` is ignored, `.env.example` holds placeholders only.
- **Do not let the code diverge from `docs/04-api-specification.md`** — amend the spec first.
- **Do not invent answers to open questions:** ❓Q3 (POI source), ❓Q5 (notification channels — blocks
  code delivery *and* password reset), ❓Q6 (EXIF default), ❓Q9 (LLM provider), ❓Q10 (accuracy bar).
  Flag them.
