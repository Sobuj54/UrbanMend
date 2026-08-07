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
- ✅ **`requires2fa` shipped hardcoded `False`; T1.7 made it a real check** and stopped `LoginView`
  issuing a full session in the same change, as this note required. See the T1.7 record below.

✅ Verified in T1.4 (2026-08-07): CSRF protection for state-changing requests. **No new code** —
`CsrfViewMiddleware` + `SessionAuthentication` were already configured from A4/T0.3. The task was
proving the mechanism works, not building it. 5 tests in `identity/tests/test_csrf.py`; full suite
**238 passed / 1 xfailed**, mypy (110 files) / ruff clean.

- **CSRF enforcement is scoped to authenticated requests.** `SessionAuthentication` enforces it only
  when it resolved the user. Pre-session endpoints (`/auth/register`, `/auth/login`) set
  `authentication_classes = []` and are exempt by design — a first-time visitor has no token to
  present. Login CSRF (tricking a victim into authenticating as the *attacker's* account) is a
  residual risk this scoping accepts; it is lower severity than the authenticated-request threat.
- **The CSRF token rotates on login**, separately from the session key. `CSRF_USE_SESSIONS = False`
  means the token rides in its own cookie, so T1.3's session-key rotation does not cover it.
  `django.contrib.auth.login()` calls `rotate_token()` — a planted token does not stay valid.
- ⚠️ **Any future view that sets `authentication_classes = []` drops CSRF enforcement with no
  warning.** The test suite catching that is the whole point of T1.4 — "DRF handles it" is an
  assumption until a test fails when it stops being true.

✅ **❓Q1 RESOLVED (2026-08-07) — the category taxonomy is confirmed**, and T0.10's `Category` is
built: `urbenmend/classification/models.py` (`Category`, `CategoryStatus`), migration `0001_initial`
seeding seven nodes, read-only-ish `CategoryAdmin`, 7 tests. `pytest` **245 passed / 1 xfailed**,
mypy/ruff clean, no drift, migration verified reversible. PRD §6.2/§15 and ops §4 amended to record
the resolution.

- **The taxonomy is the PRD §6.2 draft, confirmed as-is** — seven flat nodes: `Roads & Transport`,
  `Street Lighting`, `Water & Drainage`, `Sanitation & Waste`, `Electrical Hazards`,
  `Public Structures`, `Other / Uncategorized`. Flat, not hierarchical (§6.2 lists peers;
  data-model §5 gives Category no parent).
- ⚠️ **`Other / Uncategorized` is a required sink, not filler** (PRD §331): when LLM triage returns
  a category outside the allowed set, it coerces to `Other`. Deleting or retiring it breaks the
  fallback. A test asserts it exists and is active.
- **`slug` is the stable machine key; labels are display-only.** Code, the LLM adapter, and future
  authority-scope rows reference `slug` — renaming an English label must not break classification.
- **Bilingual labels are non-null** (`name_en`, `name_bn`, NFR-8). A test asserts `name_bn` is not a
  copy of `name_en` — a placeholder-Bangla seed would satisfy a not-null check and still ship an
  untranslated UI.
- **Lifecycle is `Active → Retired`, never deleted** (data-model §5). `CategoryAdmin` disables add
  and delete: additions come from migrations (taxonomy is reviewed data, NFR-11), and retiring
  preserves historical Report references.
- ⚠️ **The seed `RunPython` has a real reverse callable** — it deletes only the seven seeded slugs,
  never `Category.objects.all()`. Verified with a full `migrate classification zero` → `migrate`
  cycle, not by inspection; `database.md` gates reversibility and an unreversible data migration
  passes review and fails CI.
- **Still deliberately absent: the Authority↔Category M2M for BR-26 scoping.** Category now exists,
  so it is unblocked — it lands with T1.5/T1.6 as an additive migration. ✅ **Done in T1.5.**

✅ Built in T1.5 (2026-08-07): the RBAC enforcement layer. `User.category_scope` M2M, migrations
`classification/0002_category_slug` + `identity/0003_category_scope`, `AuthorizationError` and the
primitives `has_role`/`require_role`, `has_category_scope`/`require_category_scope`/
`require_scoped_visibility`, `scoped_category_ids` in `identity/services.py`, `category_scope_for`
in `identity/selectors.py`, 26 tests in `identity/tests/test_rbac.py`. `pytest` **271 passed /
1 xfailed**, mypy (112 files) / ruff clean, no drift, both migrations verified reversible.

- ⚠️ **T0.10's record claimed `Category.slug` existed; it did not.** `0001` shipped `name_en` as
  the only identifier, but API §6.2 emits `"categoryScope": ["roads","water_drainage"]` and §6.10
  addresses `PATCH /categories/{key}`. `classification/0002` adds it **nullable → backfill →
  tighten**, never one `AddField(unique=True)` against populated rows. `roads`, `water_drainage`,
  `electrical` are quoted in the spec and are therefore contract, not convention.
- ⚠️ **An empty `category_scope` grants nothing.** A provisioned-but-unscoped Authority can act on
  nothing until an Admin scopes them (BR-25). Never read empty as "unrestricted".
- ⚠️ **Admins bypass scope rather than holding every row.** `scoped_category_ids()` returns `None`
  = "apply no filter"; a row-per-category Admin loses access the moment a migration adds a node.
- ⚠️ **`has_role()` checks `status` as well as `role`** — a suspended Authority still reads
  `role == "authority"`, and honouring that would outlive the suspension until session expiry.
- ⚠️ **`403` to act (`require_category_scope`), `404` to see (`require_scoped_visibility`).** API
  §4.2 defines `404` as "absent **or hidden from this caller**"; a `403` on a scoped read confirms
  the id is a real Issue elsewhere and lets an Authority map another department's workload.
- ⚠️ **Scope is tested with `.filter(pk=...).exists()`, never `category in scope.all()`** — the
  latter caches on the instance, so a just-revoked scope keeps passing.
- **`AuthorizationError` subclasses Django's `PermissionDenied`, not DRF's** — `services.py` takes
  no DRF import, and `urbenmend_exception_handler` already maps it to `403 FORBIDDEN`.
- **Denial messages name neither the role nor the resource** — repeated across endpoints, a
  specific message maps the whole §4.2 matrix from outside.
- ⚠️ **`classification/0002`'s RunPython reverse is `noop`.** Clearing the column first
  (`update(slug="")`) fails the down migration — seven rows sharing `""` break the UNIQUE index.

✅ Built in T1.6 (2026-08-07): Authority provisioning. `provision_authority` /
`set_category_scope` / `_resolve_category_scope` / `_audit_privileged_action` and
`ProvisioningError` in `identity/services.py`, `ProvisionAuthoritySerializer` +
`UserSerializer.categoryScope`, `ProvisionAuthorityView`, `POST /users/authorities`, migration
`0004_authority_two_factor`, admin scope editing, 38 tests. `pytest` **309 passed / 1 xfailed**,
mypy (113 files) / ruff clean, no drift, `0004` verified reversible.

- ⚠️ **BR-25's audit is a structured log line, not an audit record — knowingly.** The immutable
  table is **T8.1 (P8)**, where append-only is enforced by revoking `UPDATE`/`DELETE` from the
  application role; shipping the table now without that revoke would manufacture exactly the false
  assurance NFR-10 exists to prevent. Every privileged action goes through the single funnel
  `_audit_privileged_action()`. **T8.1 replaces its body — never add a second call path beside it.**
- ⚠️ **A provisioned Authority cannot log in until T1.7.** No password is set (`create_user(
  password=None)` → unusable). The spec's body has no password field, and the alternatives are an
  Admin picking someone else's credential or a generated secret in the response body.
- ⚠️ **New authorities start `registered`, not `active`** — the work address is unproven, and BR-30
  bars notification to an unverified channel. An Admin typo must not create a live account its owner
  never hears about.
- ⚠️ **Retired categories are rejected `422`, never silently dropped.** A Retired node matches no
  Issue, so the grant would read as success and confer nothing.
- ⚠️ **This endpoint's `409` is specific; registration's is generic.** Opposite disclosure rules —
  registration is public, provisioning is Admin-only and the Admin needs to be told. Don't unify them.
- ⚠️ **Duplicate-contact check normalizes with `.lower()`, not `normalize_email`** (which lowercases
  only the domain), or the collision arrives as a `500` instead of the documented `409`. The
  `IntegrityError` catch still stays — it covers two Admins racing on one address.
- ⚠️ **`set_category_scope()` replaces, never merges** — the spec sends the whole array, so merging
  makes revocation impossible and turns narrowing into widening. `[]` legitimately revokes all.
- **Scope changes check the target's `role` column, not `has_role()`** — a suspended Authority's
  scope must stay editable, or it can only be reinstated with the wrong scope.
- ⚠️ **No `IsAdminUser` on the view** — DRF's checks `is_staff` (admin plumbing), not the domain
  `role`. `require_role(actor, Role.ADMIN)` in the service is the enforcement point (FR-3).
- **`role`/`status` in the request body are ignored** — the serializer has no such fields, so
  `POST /users/authorities` cannot be used to mint an Admin or skip verification.
- **`require_two_factor` is stored now, enforced in T1.7** — API §6.2 sends it, so discarding it
  would tell an Admin the account requires 2FA while nothing recorded it.
- ⚠️ **Admin's scope editor bypasses the service and the audit funnel** — break-glass for FR-30/31.
- ⚠️ **`users/authorities` must stay routed before any `users/<id>` pattern** — T1.9's `<str:pk>`
  would swallow it.
- ❓ **`categoryScope` reads `[]` for an Admin**, which is stored truth but not effective permission.
  API §6.2 documents only an Authority body. **Amend the spec before T1.9's `GET /users/me`** —
  not invented here.

✅ Built in T1.7 (2026-08-07): two-factor authentication (FR-4). `requires_two_factor` /
`start_partial_session` / `resolve_partial_session_user` / `enroll_totp_device` / `verify_totp` and
`TwoFactorError` + `TwoFactorEnrollmentError` in `identity/services.py`; three serializers;
`TwoFactorEnrollView` / `TwoFactorVerifyView` + `_resolve_caller`; `POST /auth/2fa/enroll` +
`POST /auth/2fa/verify`; 29 tests. **No migration** — `TOTPDevice` is django-otp's model.
`pytest` **338 passed / 1 xfailed**, mypy (114 files) / ruff clean, no drift.

- ⚠️ **API §6.1 was amended first** (the rule, followed): it specified `/auth/2fa/verify` with no way
  to obtain a device, and did not list that under its own "Missing endpoints — considered and
  resolved". `requireTwoFactor: true` was therefore a permanent lockout. `POST /auth/2fa/enroll` was
  added and `/auth/2fa/verify` documented as also confirming an enrolment — **one endpoint, because
  the first valid code *is* the proof of enrolment**; a separate `/confirm` would take the same input
  and reach the same conclusion.
- ⚠️ **The partial session works by NOT calling `django_login()`.** `SESSION_KEY` (`_auth_user_id`)
  stays unset, so `request.user` is `AnonymousUser` and every authenticated endpoint returns `401`
  automatically. **django-otp's `otp_required` decorator was rejected**: it logs the user in fully
  then gates views one at a time, so a view added later without it is reachable with one factor —
  fails open. This fails closed with no per-view gate to forget. `OTPMiddleware` stays in the stack
  but is not the enforcement point.
- ⚠️ **`start_partial_session()` calls `cycle_key()` explicitly** — `start_session()` gets rotation
  free from `django_login()`, this path does not, and without it a planted pre-login token carries
  the partial credential.
- ⚠️ **`resolve_partial_session_user()` re-fetches the user and re-checks `is_active`** — the password
  step was an earlier request, and BR-25 suspension must stop a login already in flight.
- ⚠️ **`device.key` is hex, `config_url` is base32.** Returning `device.key` as the `secret` gives a
  response whose `secret` and `otpauthUri` disagree — QR works, manual entry silently yields wrong
  codes forever. The service returns `(device, secret, otpauth_uri)`, both derived from `bin_key`.
- ⚠️ **`verify_token()` must not be reimplemented** — it stores `last_t`, the only thing stopping a
  code being replayed inside its own 30-second window. A hand-rolled comparison looks equivalent and
  has no replay protection.
- ⚠️ **A confirmed device is checked before an unconfirmed one**, or someone with a live session could
  enrol their own device and authenticate against it, sidestepping the `409`.
- ⚠️ **A flagged account with no device reads `requires_two_factor() == True`** — reading it as "not
  required" would let an Admin believe an account is protected while a password alone opens it. The
  escape is that `/auth/2fa/enroll` **accepts a partial session**. An *unconfirmed* device does not
  opt an account in — an abandoned setup must not lock anyone out.
- **`LoginView` stopped issuing a full session in the same change that made `requires2fa` real** (the
  trap T1.3 recorded). Both it and the serializer call the same service function, so the cookie and
  the body cannot disagree. **The `user` object is omitted while `requires2fa` is true** — the role is
  what a password-only holder should not learn; `null` leaks the same distinction by shape.
- **`enroll_totp_device()` has no role check** — a citizen protecting their own account is not
  escalation; authorization is "self" and structural.
- ⚠️ **The two 2FA routes are the only ones accepting a non-authenticated session.** Anything else
  added under `auth/2fa/` needs that decision made deliberately.
- ❓ **`/auth/password/forgot`·`/reset` is unbuilt and now explicitly unowned.** `api/urls.py` used to
  route it to T1.7; the plan's T1.7 row is 2FA-only and reset traces to FR-1 (T1.2), where it was
  never built. Delivery is blocked on ❓Q5. **A provisioned Authority still has no way to set a first
  password** — T1.6 creates them with an unusable one.

✅ Built in T1.8 (2026-08-07): login/OTP rate limiting + lockout backoff (FR-4, API §4.5). New
`urbenmend/api/throttling.py` — `ScopedWindowRateThrottle`, `AuthAnonRateThrottle`,
`AuthIdentityRateThrottle`, `AuthUserRateThrottle`, `RateLimitHeadersMixin`,
`clear_identity_throttle`; `AUTH_THROTTLE_RATES` in `settings/base.py`; throttles on all five auth
views; **new root `conftest.py`**; 21 tests in `identity/tests/test_rate_limiting.py`.
**No migration.** `pytest` **359 passed / 1 xfailed**, mypy (116 files) / ruff clean, no drift,
`check --deploy` clean.

- ⚠️ **Lockout is throttle-only backoff — no per-account lock state, deliberately.** FR-4 says
  "lockout/backoff" without defining it. Persistent lockout is a targeted DoS: anyone knowing an
  Authority's email could hold them out on demand. Failed logins consume a bucket, success clears
  it. **`403 ACCOUNT_LOCKED` remains a `status` denial only** (T1.3) — never reuse it for throttling,
  and never reuse `UserStatus.SUSPENDED`, which is a BR-25 moderation action.
- ⚠️ **The rates are our policy, not spec-derived** (`api-conventions.md` lists numeric limits under
  "do not invent"). `auth_anon` `10/15m` per IP, `auth_identity` `5/15m` per identifier, `auth_user`
  `20/15m` per session — in settings, env-overridable (NFR-11). T1.2's precedent.
- ⚠️ **DRF's `parse_rate` reads only `period[0]` — `"5/15m"` silently means 5 per *minute*.** 15×
  tighter than written, nothing indicates it. `ScopedWindowRateThrottle` overrides it; a plain
  `"10/hour"` still behaves as DRF does. Verified against installed source.
- ⚠️ **DRF emits no `RateLimit-Limit`/`-Remaining`/`-Reset`** — API §4.5 requires all three on every
  limited endpoint (DRF sets only `Retry-After`, only on 429). `check_throttles()` stores nothing on
  the request, so `RateLimitHeadersMixin` overrides **`get_throttles()`** to capture the instances
  DRF actually uses. Reading invented `request.throttle_*` attributes emits nothing, silently.
  The advertised bucket is the one with least **headroom**, not the smallest limit.
- ⚠️ **`AuthIdentityRateThrottle` keys on the submitted `identifier`, SHA-256'd, never raw** — cache
  keys surface in `redis-cli KEYS` and dumps, and an email there is PII (NFR-12). Keying on
  `request.user` would throttle only *after* a successful password check, i.e. never during the
  attack. Normalized first or casing multiplies the allowance. Parse failures return `None` rather
  than raising — an exception aborts `check_throttles()` and the per-IP bucket never counts.
- ⚠️ **`clear_identity_throttle()` is success-path only and clears the identifier bucket alone.**
  Calling it earlier or in a `finally` removes the counter entirely and every happy-path test still
  passes. The per-IP bucket survives success on purpose.
- ⚠️ **Throttle runs before the credential check** — a correct password while throttled is refused.
- ⚠️ **A partial post-password session is unauthenticated (T1.7), so 2FA code-guessing lands in
  `auth_anon`, not `auth_user`.** That is the bucket to tighten for OTP. `verify_totp()` has no
  per-account attempt counter of its own — `verify_token()` blocks replay, not a keyspace walk.
- ⚠️ **Rates read at instantiation, not bound as a class attribute** — DRF binds `THROTTLE_RATES` at
  import, where `override_settings` cannot reach, making the 429 path untestable.
- ⚠️ **`DEFAULT_THROTTLE_CLASSES` stays unset** — a global default would throttle the public map and
  issue list, which §4.5 does not ask for and Q7 makes unauthenticated. Opt in per view.
- ⚠️ **Throttle state is NOT rolled back between tests** — it lives in Redis, not the DB. The new
  root `conftest.py` clears the cache autouse around every test; without it, adding throttles turned
  **27 existing tests red** with order-dependent 429s. Any future throttled endpoint depends on it.
  Safe only because sessions are `cached_db`.
- **No spec amendment owed** — §4.5 and §6.1's `429`s already specify this; only the numbers are open.

✅ Built in T1.9 (2026-08-07): `/users/me` — profile read/update + account deletion → PII
anonymization (P6, BR-33, C-14). `update_profile` / `anonymize_account` + `ProfileUpdateError` /
`AccountDeletionError` in `identity/services.py`; `ProfileUpdateSerializer`; `MeView`
(GET/PATCH/DELETE); `GET`·`PATCH`·`DELETE /users/me`; 36 tests in `identity/tests/test_profile.py`.
**No migration.** `pytest` **395 passed / 1 xfailed**, mypy (117 files) / ruff clean, no drift,
`check --deploy` clean.

- ⚠️ **API §6.2 was amended first, in three places** (the rule, followed): one response shape for all
  three roles; a table for what `categoryScope: []` means per role; and Citizen-only `DELETE`.
  ✅ **This closes the T1.6 ❓** — that record said "amend the spec before T1.9's `GET /users/me`".
- ⚠️ **An Admin's `categoryScope: []` and an unscoped Authority's `[]` are identical JSON with
  opposite meanings** — unrestricted vs. permitted-nothing. `role` is the only disambiguator. Emitted
  for every role anyway so the shape stays stable; a client must not read capability from it alone.
- ⚠️ **`DELETE /users/me` is Citizen-only**, and that came from the data-model, not §6.2: the
  ownership matrix grants an Authority `RU`, not `D`. An Authority erasing an audited grant (FR-2,
  BR-25) or an Admin removing the last provisioner are both `403`. The alternative path is
  `PATCH /users/{id} {"status":"deprovisioned"}` — now named in the spec.
- ⚠️ **Anonymization nulls PII in the *same* `save()` as the status flip.** Two UPDATEs leave a
  window where a crash yields a `deleted` row still carrying a live email — and the constraint's
  DELETED branch accepts it. `status=DELETED` is that escape hatch (A6); **do not tighten it.**
- ⚠️ **The row is retained, never deleted** (C-14). `test_anonymization_retains_the_row` is what fails
  if this is "simplified" to `user.delete()` — every PII assertion would still pass.
- ⚠️ **The `VerificationCode`/`TOTPDevice` deletes are explicit because no cascade fires** — nothing
  is deleted here, the user row survives. Implicit would leave a live TOTP secret on a dead account.
- ⚠️ **Sessions are revoked inside the transaction; `is_active` is not a substitute.**
  `status=DELETED` stops *new* authentications at commit, but a live session runs to expiry (Arch §8).
- ⚠️ **`202` is written to a caller that can no longer authenticate.** It reflects that
  retained-record anonymization may extend past the response (P2/P3), not that the account works.
- ⚠️ **`PATCH /users/me` rejects unknown fields rather than dropping them.** DRF's default makes
  `PATCH {"role":"admin"}` a `200` with an unchanged body — indistinguishable from success. Allowed
  keys are derived from the declared fields in both spellings: the camelCase mixin rewrites keys
  before `validate()` runs, while `initial_data` keeps the client's original.
- ⚠️ **`ProfileUpdateSerializer` is a plain `Serializer`, never a `ModelSerializer` over `User`** —
  otherwise `role`/`status`/`is_staff`/`require_two_factor` are one `fields` edit from being
  self-assignable. Escalation must require adding a field, not forgetting to exclude one.
- ⚠️ **`email` is not updatable here** — it is the password-reset address, so a self-service change
  turns one borrowed session into permanent takeover. `update_profile` has no such parameter; the
  test asserts the *signature*, because the guarantee is structural.
- ⚠️ **Any submitted `phone` clears `phone_verified_at`, even when unchanged** (BR-30, fail closed).
  Skipping on equality keeps a stale proof alive for a number that changed hands and came back.
- ⚠️ **`""` clears the phone; `null` is refused** — aliasing them sends `null` into the E.164
  validator as a `500`. `save()` normalizes `""` to the NULL the UNIQUE index needs.
- ⚠️ **Clearing the last contact channel is `422`, not `400`** — a business rule, not a bad body.
- ⚠️ **The `409` check uses `.exclude(pk=user.pk)`**, or an unchanged resubmission conflicts with
  itself. The `IntegrityError` catch still stays — it closes the check-to-UPDATE race.
- **`403` is Django's `PermissionDenied`, not a bespoke code** — §6.2 names none for this endpoint,
  so it renders as the generic `FORBIDDEN`. Contrast `ACCOUNT_LOCKED`, which §6.1 does name.
- **`MeView` carries `auth_user` only** — `auth_anon` is sized for pre-session attempts, and applying
  it here would let one user's profile edits exhaust the login allowance for everyone behind a NAT.
- ⚠️ **`users/me` must stay routed before any `users/<str:pk>`** — same trap as `users/authorities`.
- ❓ **`GET /users` and `PATCH /users/{id}` (both Admin, API §6.2) are unbuilt and now explicitly
  unowned.** `api/urls.py` used to point them at T1.9; T1.9 scoped to `/users/me` only. They need a
  task ID — the same treatment `/auth/password/forgot`·`/reset` got. Note `PATCH /users/{id}` is the
  documented route for Authority deprovisioning and for Admin-side email changes, so **two T1.9
  decisions currently have no implemented alternative path.**

✅ Built in T2.1 (2026-08-07): **the `Report` entity + its validation primitives** — and the `geo`
app that makes a location checkable. `geo/models.py` (`CityBoundary`), `geo/selectors.py`
(`active_city_boundary` / `is_within_city` / `BoundaryUnavailable`), `reporting/models.py` (`Report`,
`ReportStatus`, `SeveritySignal`, `SEVERITY_RANK`, `ClassificationSource`), `reporting/services.py`
(`create_report` / `validate_location` / `validate_report_content` / `ReportValidationError`),
`OutOfCity` in `api/exceptions.py`, `geo/admin.py` + `reporting/admin.py`, migrations
`geo/0001_initial` + `geo/0002_seed_city_boundary` + `reporting/0001_initial`,
`docs/city-boundary/dhaka-demo.geojson`, four `factory_boy` factory modules, 59 tests.
**No endpoint** — `POST /reports` is T2.2. `pytest` **454 passed / 1 xfailed**, mypy (124 files) /
ruff (132 files) clean, no drift, `check --deploy` clean, both apps verified reversible.

- ⚠️ **Intake fails closed when no boundary is configured — Arch §409's degradation is declined.**
  §409 sanctions skipping a check whose dependency is missing; C-11 says an out-of-city location "is
  not accepted". `active_city_boundary()` **raises rather than returning `None`**, so no caller can
  write `if boundary and not contains(...)` — which accepts every location on Earth the moment the
  table empties. An empty boundary table rejects everything loudly instead.
- ⚠️ **`GistIndex`, never `models.Index`, on `location`.** `models.Index` emits a B-tree, which
  cannot serve `ST_Within`/`ST_DWithin` — spatial queries silently become sequential scans while the
  migration reads as indexed. Paired with `spatial_index=False` to avoid a duplicate auto-named
  index. BR-35, T4.4 clustering, and the T7.4 map bbox all rest on this line.
- ⚠️ **`OutOfCity` (`422`) is its own type, not a `ReportValidationError` (`400`).** A well-formed
  coordinate outside the city is a business-rule violation, not a malformed body; collapsing them
  leaves a client unable to tell "fix your JSON" from "we do not serve your city".
- ⚠️ **Order is authorization → location → content, and it is observable.** A submission that is both
  out-of-city and under-described gets the `422`: reporting the description first sends a citizen off
  to write prose about a place UrbanMend does not serve. A non-Citizen gets `403` and learns nothing
  about which other rules they broke.
- ⚠️ **The boundary is seeded data, not code** (NFR-11). `geo/0002` loads the GeoJSON via
  `apps.get_model()` with a **real reverse** deleting only `Dhaka (development stand-in)`. GEOS is
  imported *inside* the function so `makemigrations --check` runs without the library. Replacement is
  **add-and-retire, never edit-in-place** — `CityBoundaryAdmin` makes `area` read-only on change,
  writable on add, delete denied. Exactly one row must be active: the selector raises on zero *and*
  on two, so a half-finished swap errors instead of validating against whichever row sorted first.
- ⚠️ **`ReportStatus` holds no Issue-workflow value, asserted by name.** `Acknowledged`/`In Progress`/
  `Resolved`/`Closed` are Issue statuses (PRD §6.3) — adding one gives two rows one answer to "is this
  fixed?". `Draft` is omitted too (FR-8 SHOULD; a PWA concern).
- ⚠️ **`SeveritySignal` lives in `reporting` and is shared with `issues`; four bands, not three.** Two
  independently declared enums would let one gain a band the other lacks, making BR-11's `max()`
  undefined. `SEVERITY_RANK` exists **only** so "highest" is computable — not a score (FR-21), not
  tunable, not exposed, never combined with corroboration or proximity (C-10, display-only).
- ⚠️ **`is_classified` keys on `classified_at`, not `category`.** A citizen's category *hint* fills
  `category` at intake, so keying on it would mark an unclassified report classified and T3.5's
  worker would skip it forever, silently.
- ⚠️ **A hint is recorded with `classification_source = citizen` and is not a classification.** An
  unknown *or retired* slug is `422`, never coerced to `Other` — that coercion (BR-7) is for LLM
  output, where an off-taxonomy value means an unusable answer. A human on a retired node is running
  a stale client; filing under `Other` would lose information they could have fixed.
- ⚠️ **`# noqa: DJ001` on `severity_signal`/`classification_source` is deliberate.** `""` is not a
  member of `SeveritySignal` — an undeclared fifth band that validates only because Django skips
  blanks, and `SEVERITY_RANK[""]` raises `KeyError` inside T4.6's `max()` at runtime. `confidence` is
  a `FloatField` that must be NULL, so mixing conventions in one block makes T3.5's queue query
  depend on knowing which applies per column. (DJ001 never fired on `identity.email`/`phone` because
  it exempts `unique=True` — know that before "fixing" it.)
- ⚠️ **`author` is `PROTECT`** (C-14): BR-33 deletion is anonymization, so the cascade should never
  fire; `PROTECT` makes a hard delete fail loudly instead of erasing the reports that give an Issue
  its corroboration count (FR-16). **The Issue FK is deliberately absent until T4.1** — a test
  asserts the absence so whoever adds it meets BR-6's at-most-one rule, not a loose UUID column.
- ⚠️ **A registered-but-unverified citizen may submit.** BR-30 gates *notification* on verification,
  not intake, and the unverified capability set is explicitly unspecified — don't narrow it here.
- ⚠️ **`create_report()` takes no `status` parameter, and the test asserts the *signature*** — the
  guarantee is that the parameter cannot exist, or a caller marks a report `triaged` and skips the
  pipeline. Same technique as T1.9's `update_profile`/`email`.
- ⚠️ **`ReportFactory` bypasses `create_report()` and builds *unclassified*.** Routing fixtures
  through the service would make one validation bug fail unrelated suites and force every test to
  seed a boundary. Unclassified because BR-9 — a pre-filled factory makes the async path untestable
  from its real starting state. **`CityBoundaryFactory` defaults `is_active = False`, unlike the
  model**, since the migration already seeds one active row and the selector raises on two.
- ⚠️ **Two narrowly-scoped mypy settings for `factory_boy`** (Celery-decorator precedent):
  `untyped_calls_exclude = ["factory"]` (its declaration helpers are unannotated despite `py.typed`)
  and `implicit_reexport` for `factory.*` (no `__all__` — verified, not assumed). **Neither excuses
  the `F()` shorthand**: `FactoryMetaClass.__call__` is unannotated, so `UserFactory()` types as the
  *factory* and defeats checking on every fixture. Use `F.create()` / `F.build()`, annotated `-> T`.
- ⚠️ **Green output can prove nothing.** The first reversibility run reported "No migrations to
  apply" because they had never been applied; the real test is `[X]` → `[ ]` → `[X]`. Same lesson as
  A7's `CreateExtension` no-op against the compose DB.

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
- Authorization is enforced in the **service layer**, on every mutating and sensitive-read action —
  call the T1.5 primitives in `identity/services.py`; do not reimplement a role or scope check
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
- **Do not invent answers to open questions** Q3 (POI source), Q5 (notification channels), Q6 (EXIF
  default), Q10 (accuracy bar). Flag them.

## Path-scoped rules

`.claude/rules/` loads automatically by file pattern: `api-conventions.md`, `auth.md`,
`database.md`, `async-worker.md`, `testing.md`.
