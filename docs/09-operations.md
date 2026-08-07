# UrbanMend — Operations (DC-1)

> **DC-1** [doc: Plan §8.2] — environment/setup notes, runbook skeleton, migration guide.
> Written at the end of P0 (2026-08-05). This is the **M0 gate's** documentation deliverable.

| | |
|---|---|
| **Audience** | Anyone setting the backend up locally, or operating it once deployed |
| **Scope** | Backend only (API process + Worker process + backing services) |
| **Status** | §1 and §3 describe **what exists and has been run**. §2 is a **skeleton** — see the warning there. |

⚠️ **What this document is not.** DC-1 is the *first* of seven documentation checkpoints. The full
deployment runbook, rollback, restore and on-call guide is **DC-6, at the end of P10**. §2 below is
deliberately a skeleton: no environment has been deployed, so a detailed runbook would be fiction.
Sections marked **(DC-6)** are placeholders with the questions they must answer, not procedures to
follow.

Related: [06-devops-guide.md](06-devops-guide.md) is the *design* (containers, CI, K8s,
observability); this document is the *operator's* view of what was actually built.

---

## 1. Environment & setup

### 1.1 Prerequisites

Only **Docker Desktop** (or Docker Engine + Compose v2) and **git**.

⚠️ **A native Python install is not a supported path, and is not merely inconvenient.** GeoDjango
`dlopen()`s GDAL/GEOS/PROJ at runtime, which is awkward to install natively on Windows — this
team's platform. Compose is therefore the **mandated** local environment
[doc: DevOps §3.1, Plan P0], not a convenience. Every command below runs inside a container.

Python 3.13 / Django 5.2.16 LTS / DRF 3.17.1 are pinned in the image; you do not install them.
⚠️ Python 3.13 is a **ceiling, not a preference** — `djangorestframework-gis` 1.2.1 caps there.

### 1.2 First run

```bash
git clone <repo> && cd <repo>
cp .env.example .env.local          # placeholders only; see §1.3
docker compose up -d --build        # db, redis, storage, api, worker
docker compose run --rm api python manage.py migrate
```

⚠️ **`migrate` is a separate step and always will be.** It is never in the Dockerfile or the
container entrypoint [doc: DevOps §7, database.md] — N replicas would race each other on rollout.
Deployed, it is a **pre-deploy Job**. See §3.

Verify:

```bash
curl -s localhost:8080/api/v1/health   # {"status":"ok","dependencies":{...}}
```

### 1.3 Configuration

Config is environment variables via `django-environ`. `.env.local` is git-ignored; `.env.example`
holds **placeholders only**. ⚠️ **Never commit a real secret.**

**Required — no fallback in `base`/`prod`:**

| Variable | Notes |
|---|---|
| `DJANGO_SECRET_KEY` | Missing ⇒ startup failure, by design |
| `DATABASE_URL` | ⚠️ **`postgis://` scheme, not `postgres://`** |

⚠️ The `postgis://` scheme is what selects `django.contrib.gis.db.backends.postgis`. `base.py`
asserts the resolved engine and raises immediately if it is wrong — without that check, the wrong
scheme surfaces much later as a confusing "unknown field type" error.

⚠️ **`dev.py` supplies local-only fallbacks for both**, so a fresh clone can lint and test without
provisioning a secret. **`prod.py` deliberately has none — never add one.** A deployment with a
missing secret must fail to boot rather than run on a publicly-known key.

**Optional (defaults in `base.py`):** `REDIS_URL` (`redis://redis:6379/0`), `CELERY_BROKER_URL`
(`…/1`), `STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`,
`AWS_REGION`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`.

**Production-only:** `DJANGO_ALLOWED_HOSTS` (**required, no default**),
`DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_HSTS_SECONDS`, `DJANGO_LOG_LEVEL`, `DATABASE_SSLMODE`.

### 1.4 Settings modules

`base` → `dev` → `prod`, plus `build`. ⚠️ **There is no `settings.local`** — DevOps §3.2's naming
was amended to `dev` in A1. Do not reintroduce it.

| Module | Used by |
|---|---|
| `urbenmend.settings.dev` | Local Compose and `manage.py` default. **Never deployed.** |
| `urbenmend.settings.prod` | Every deployed environment. No secret fallbacks; strict cookies/HSTS. |
| `urbenmend.settings.build` | Build-time `collectstatic` only — injects throwaway values so the image build needs no secrets. |

### 1.5 Services

| Service | Image | Port | Purpose |
|---|---|---|---|
| `db` | `postgis/postgis:17-3.5` | 5432 | ⚠️ **Not plain `postgres`** — the extension must exist before the first migration enables it |
| `redis` | `redis:8-alpine` | 6379 | Cache, **sessions**, rate-limit store (db 0); Celery broker (db 1) |
| `storage` | `minio/minio` | 9000/9001 | S3-compatible photo storage |
| `api` | built here | 8080 | uvicorn/ASGI |
| `worker` | built here | — | Celery worker + beat |

⚠️ **`worker` runs beat in-process (`-B`) LOCALLY ONLY.** Deployed, beat is a **separate
single-replica Deployment, `strategy: Recreate`, never autoscaled** — two schedulers double-fire the
outbox relay (T6.2) and every periodic job [doc: DevOps §3.1/§6.1, async-worker.md].

`docker-compose.override.yml` applies automatically and is local-only: it swaps both processes to
the `dev` image target, bind-mounts source, and adds `--reload`. ⚠️ Celery has **no** reloader — a
worker code change needs `docker compose restart worker`.

### 1.6 Everyday commands

All gates run **inside the `dev` image**, which is what CI does — see §1.7.

```bash
docker compose exec api ruff check
docker compose exec api ruff format --check
docker compose exec api mypy                                 # strict
docker compose exec api pytest
docker compose exec api python manage.py makemigrations --check --dry-run
docker compose exec api python manage.py check --deploy
docker compose exec api python manage.py createsuperuser      # Django admin (FR-30/31)
```

⚠️ One command per line, deliberately. `docker compose exec api ruff check && ruff format --check`
runs the **second command on the host**, where ruff is not installed — it either fails confusingly
or silently checks nothing. To chain inside the container, quote the whole thing:
`docker compose exec api sh -c 'ruff check && ruff format --check'`.

Verified 2026-08-05 against the running stack: ruff clean, mypy clean (105 files), `pytest`
**183 passed / 1 xfailed**, `makemigrations --check` "No changes detected",
`GET /api/v1/health` → `200 {"status":"ok","dependencies":{"database":{"status":"ok"},"cache":{"status":"ok"}}}`.

⚠️ The 1 xfail is **intentional** — `test_clustering_concurrency.py` is the P4 concurrency test
written red in P0 (A10, R-2). It is `xfail(strict=True)`, so it fails the build if it ever passes
unexpectedly. Remove the marker in T4.4, not before.

### 1.7 Why everything runs in a container

⚠️ Not stylistic. The runtime library package names are **Debian 13 (trixie)** specific
(`libgdal36`, `libgeos-c1t64`, `libproj25`). CI's `ubuntu-latest` is Ubuntu noble, where the same
libraries are named differently (`libgdal34`, `libgeos-c1v5`). Installing them by hand on the
runner would fork the dependency set the deployed image actually uses. Running every gate inside
the image keeps CI, local and production identical.

Dockerfile stages: `deps` → `runtime` → `dev`. ⚠️ **`dev` is deliberately last** so
`--target runtime` can never pick up pytest/ruff/mypy.

### 1.8 Dependency changes

Dependencies are compiled with `pip-compile`; **never hand-edit a `.txt`**.

```bash
# edit requirements/base.in or dev.in, then:
pip-compile --generate-hashes requirements/base.in
pip-compile --generate-hashes --allow-unsafe requirements/dev.in   # ⚠️ dev needs --allow-unsafe
docker compose build
```

---

## 2. Runbook skeleton

⚠️ **SKELETON — nothing here has been executed against a deployed environment, because none
exists.** Every subsection marked **(DC-6)** states the questions it must answer rather than a
procedure. Do not follow it as if it were verified. It is completed at **DC-6, end of P10**
[doc: Plan §8.2].

### 2.1 What runs

Deployed, **four workloads run the same image**, differing only in `command` [doc: DevOps §6.1]:

| Workload | Replicas | Command |
|---|---|---|
| `api` | 2+ (HPA on CPU/RPS) | `uvicorn urbenmend.asgi:application --host 0.0.0.0 --port 8080` |
| `worker` | 1–2, scaled on **queue depth** (not CPU) | `celery -A urbenmend worker --loglevel=info` |
| `beat` | ⚠️ **EXACTLY 1**, `strategy: Recreate`, never autoscaled | `celery -A urbenmend beat --loglevel=info` |
| `migrate-<sha>` | run-once Job, pre-deploy | `python manage.py migrate` |

⚠️ **Note the worker command drops `-B`.** Locally, `docker-compose.yml` runs
`celery -A urbenmend worker -B` — beat in-process. Deployed, beat is its own Deployment and the
worker must **not** carry `-B`, or every worker replica becomes a scheduler. Two schedulers
double-fire the outbox relay (T6.2) and every periodic job [doc: DevOps §3.1/§6.1,
async-worker.md]. `strategy: Recreate` matters too — a rolling update would briefly run two.

⚠️ ASGI is **required, not a preference**: the SSE notification stream (T6.8) needs it. Under WSGI
each open stream pins a worker thread.

⚠️ **The worker deployment must be rolled with the API**, never left behind [doc: DevOps §5.2].

⚠️ **`readOnlyRootFilesystem: true` needs an `emptyDir` at `/tmp`** or Django file uploads break
(mandatory on the worker; `TMPDIR` may point at the mounted volume).

### 2.2 Health and readiness

`GET /api/v1/health` — unauthenticated by design (a probe whose credential expired would mark
healthy pods not-ready). Returns a three-state verdict:

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | All dependencies reachable |
| `degraded` | **200** | An *optional* dependency failed — a feature is degraded (NFR-4 working as designed) |
| `unavailable` | 503 | A *required* dependency failed — pod cannot serve |

⚠️ **Only a required failure returns 503**, and a 503 pulls the pod from the load balancer
[doc: DevOps §8.4]. Marking an optional dependency required would take the deployment offline for a
failure NFR-4 says must merely degrade. Currently required: **database** and **cache** (cache is
required *because sessions live in it*). LLM and geocoder probes land here in P2/P3 as optional.

⚠️ Failure `detail` is deliberately generic. Driver errors carry host, database name and user from
the DSN, and this endpoint is public — **never widen it to include the exception text.**

### 2.3 Logs

Structured JSON to stdout/stderr, never to files in the container. Every line carries `timestamp`,
`level`, `traceId`, `service`, `message`.

Correlate a request with the work it queued using `traceId`: the API stamps it, and it propagates
into Celery task **headers** and is re-bound in the worker.

⚠️ **`request.path` is logged, never the query string.** Filters carry `?q=` search text, and
NFR-12/P6 treat report content as personal data.

⚠️ Inbound `X-Trace-Id` values are **rejected, not escaped**, when they contain CRLF/NUL/whitespace
or exceed 128 chars — the value is echoed into a response header and written to logs.

### 2.4 Metrics

`/metrics` (Prometheus, via `django-prometheus`). ⚠️ **Must NOT be reachable through the Ingress** —
exposing it publishes the operational picture of the deployment [doc: DevOps §8.2/§9]. Django
cannot enforce this; it is an Ingress/proxy concern. Pod-port scrape only.

### 2.5 Deploy **(DC-6)**

Must answer: the pre-deploy `migrate` Job (§3.3); rollout order (migrate → API+worker together);
readiness gating; SHA-tagged image selection. ⚠️ **Never deploy `latest`** — a moving tag makes the
deployed revision unknowable and rollback unrepeatable [doc: DevOps §2.3].

### 2.6 Rollback **(DC-6)**

Must answer: how to roll back **code** when the schema has already moved forward. ⚠️ This is why
migrations must be **backward-compatible** (§3.2) — the previous image must run against the new
schema. Also: what is *not* rollable (a dropped column), and the decision rule for
roll-back-vs-roll-forward.

### 2.7 Backup & restore **(DC-6)**

Must answer: Postgres backup schedule/retention and a **rehearsed** restore; object-storage
durability for report photos; what an Issue's history means if media is lost. ⚠️ Untested backups
are not backups.

### 2.8 On-call **(DC-6)**

Must answer: alert thresholds (HTTP p99 > 500 ms per NFR-2, queue depth, LLM cost caps per NFR-13),
escalation, and per-alert first response.

### 2.9 Common operations

⚠️ Unlike the rest of §2, this table is **not** a placeholder — it is condensed from
[06-devops-guide.md](06-devops-guide.md) §9.1, which already specifies these. It is still
**unrehearsed**: no environment exists to have practised them against. DC-6 turns each row into a
step-by-step procedure.

| Situation | First response |
|---|---|
| Deploy to production | Pull SHA tag → `migrate` pre-deploy Job → rolling update → smoke test → verify metrics |
| Rollback | Re-deploy previous SHA tag → reverse schema **only if reversible** (§3) → verify health → investigate |
| LLM outage | ⚠️ **No manual action.** Keyword fallback activates automatically; intake and the queue must never block, and the API still returns `202`. Monitor the classification-source metric; act only if the fallback also fails |
| Worker crash / queue backlog | Check pod logs, restart pod, watch queue depth drain. ⚠️ **Check beat is alive first** (outbox backlog metric) — a stalled beat is silent: no queue grows and no error is logged, notifications simply stop |
| DB failover | Promote replica → update the `DATABASE_URL` secret → rolling restart of api **+ worker + beat** |
| High error rate | Group logs by `traceId`; check DB/Redis health; check the LLM cost cap |
| Security incident | ⚠️ Revoke sessions server-side by deleting `django_session` rows — this is why sessions are used and **not JWT** (BR-25/33, Arch §8). Rotate secrets including `DJANGO_SECRET_KEY`; review the audit log |
| Beat scaled past 1 replica | Duplicate Issues / double-fired outbox. Scale back to exactly 1 (§2.1) |

⚠️ **The outbox backlog metric — age of the oldest unrelayed row — is the only signal that
distinguishes "nothing to send" from "the relay is dead."** Queue depth will not tell you.

---

## 3. Migration guide

### 3.1 The rules

- **Migrations are code** — committed, reviewed in PRs, ⚠️ **never applied manually in production**.
- A generated migration is a **draft to be reviewed**, not an artifact to trust. Check operation
  order [doc: DevOps §7].
- ⚠️ **Never edit a migration already applied to a shared environment.**
- ⚠️ **Never run `migrate` in the Dockerfile or entrypoint.** Pre-deploy Job only.
- `makemigrations --check --dry-run` is a **CI gate** (stage 2). It catches a model edited without
  its migration — which passes locally against an already-migrated dev DB and then fails on a fresh
  deploy.
- Keep every migration reversible; CI tests both directions.
- ⚠️ `RunPython` is **not** reversible unless you supply a reverse callable, and data migrations
  **must** use `apps.get_model(...)` and **must not** import application code — including
  `services.py`/`selectors.py`. They receive historical model states.

### 3.2 Zero-downtime

⚠️ **Backward-compatible migrations only.** During a rolling update, old and new code run against
one schema simultaneously. A rename is therefore **always three deploys** — add → dual-write and
backfill → drop — never one.

⚠️ **Long-lived locks are the real hazard.** Adding an index to a populated table (the GiST spatial
indexes of T4.2 especially) locks writes for the duration. Use `AddIndexConcurrently` in a
non-atomic migration (`atomic = False`) so the deploy does not block report submission. Split
`ADD CONSTRAINT` from `VALIDATE CONSTRAINT` likewise.

### 3.3 Applying

```bash
docker compose run --rm api python manage.py migrate     # local
```

Deployed: a **pre-deploy Job**, not an init container — an init container runs once per pod, so N
replicas race. Old pods keep serving until new pods pass readiness.

### 3.4 The PostGIS baseline ⚠️

`identity/0001_initial.py` leads with `CreateExtension("postgis")`, then creates `User`.

- ⚠️ **It must stay the first operation of the first migration.** It lives in `identity` — not
  `geo`/`reporting`, which will own the geometry — because Django orders by the **dependency
  graph**, not app name, and `identity.0001` is the earliest project-owned node (`AUTH_USER_MODEL`
  points at it). A geometry-bearing app must name it in `dependencies` if it has no other path.
- ⚠️ **`identity/0001` is frozen.** It has been applied. It was hand-edited before that (the
  documented posture: a generated migration is a draft).
- ⚠️ **Reversing it DROPs the extension** — destructive on any database holding geometry.
  Deployment is **forward-only**.
- The database role running the first migration must be able to `CREATE EXTENSION`.

⚠️ **`postgis/postgis` pre-creates the extension, so `migrate` against the Compose DB cannot fail
and proves nothing** — `sqlmigrate` shows the operation as `-- (no-op)` there. Verify on a fresh
`CREATE DATABASE` holding only `plpgsql`. CI does this (stage 5, `migration_probe`).

### 3.5 The CI role ⚠️

The CI database user is a **superuser** and is deliberately **not** the runtime application role.
The test database is built from zero every run and must `CREATE EXTENSION`; separately, T8.1
enforces the append-only rule on status/audit events by **revoking `UPDATE`/`DELETE` from the
application role** — if CI ran as that role, the revoke script itself would be untestable
[doc: testing.md, database.md].

### 3.6 Deletion

⚠️ **No hard deletes.** Categories, POIs and severity keywords use `Active → Retired`. Issues are
never hard-deleted; moderation hides content (FR-31). Deleting a user **anonymizes** — it must not
orphan or destroy public Issue history (C-14, BR-33).

⚠️ The `identity_user_has_contact_or_anonymized` constraint has a `status=deleted` escape hatch.
**Without it the C-14 anonymization would be impossible — do not tighten it.** Absence of a contact
is **`NULL`, never `""`**: Postgres allows many NULLs under UNIQUE but only one `""`.

---

## 4. Open questions affecting operations

⚠️ **Unresolved. Do not invent answers — raise them** [doc: CLAUDE.md, Plan §10].

| ID | Question | Operational impact |
|---|---|---|
| — | **Cloud host** | Unpinned. Blocks §2.5–2.8 concretely. |
| Q3 | POI data source | Display-only, but an ingest job to operate |
| Q5 | Notification channels | Determines SMS provider and its cost/rate ceilings |
| Q6 | EXIF default | ⚠️ Privacy-affecting (BR-4: EXIF stripped by default) |
| Q10 | Accuracy bar | Sets the LLM-quality alert threshold |

**Resolved:** Q1 (taxonomy — confirmed 2026-08-07 as the seven-node PRD §6.2 draft; seeded in
`classification/0001`), Q2 (severity is Critical/High/Medium/Low), Q9 (LLM provider **deferred** —
the adapter stays provider-agnostic and the product never hard-depends on the external API, NFR-4).
