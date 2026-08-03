# UrbanMend — DevOps Guide

> Platform-agnostic, **stack-targeted** guide covering containerisation, CI/CD, orchestration, and deployment for the UrbanMend backend (API process + Worker process) on the committed **Python + Django + DRF** stack (ADR-001).

|                     |                                                                                               |
| ------------------- | --------------------------------------------------------------------------------------------- |
| **Document**        | `docs/06-devops-guide.md`                                                                     |
| **Version**         | 1.1 (retargeted to the committed Django + DRF stack per ADR-001)                              |
| **Status**          | Planning phase                                                                                |
| **Author role**     | DevOps / Platform Engineer                                                                    |
| **Date**            | 2026-08-03                                                                                    |
| **Source of truth** | `02-architecture.md` · `04-api-specification.md` · `05-project-plan.md` · `07-adr-001-app-framework.md` |
| **Scope**           | Backend only (API process + Worker process + supporting services). Client/UI is out of scope. |

### Ground rules

- All tooling choices below are **illustrative patterns**, not mandates. Swap any named tool for an equivalent that fits your platform. The **Django-specific** container, migration, and hardening patterns in §2, §7, and §9 *are* mandated by ADR-001.
- Every step traces to a requirement in the approved docs (`NFR-x`, `BR-x`, `RISK-x`).
- **No secrets in code or images.** All credentials are injected at runtime via environment variables or a secrets manager.

---

## 1. Repository & Branch Strategy

```
main          ← production-ready; protected; deploys to prod on tag
staging       ← integration branch; auto-deploys to staging env
feature/*     ← short-lived; merged to staging via PR
hotfix/*      ← branched from main; merged to main + staging
```

- **Trunk-based development** is preferred for a 2-person team: short-lived branches, frequent merges, feature flags for incomplete work.
- Branch protection rules: require passing CI, at least 1 review, no direct push to `main`.

---

## 2. Containerisation (Docker)

### 2.1 Image structure

Two images, one per process:

```
urbenmend/api     ← HTTP API process  (Architecture §2.1) — ASGI server (uvicorn)
urbenmend/worker  ← Async job worker  (Architecture §2.2) — Celery worker + beat
```

Both processes run **the same Django codebase and the same `DJANGO_SETTINGS_MODULE`**; they differ only in entrypoint. Build **one image** and select the process at run time via the container command (the deployment sets `command:`), rather than maintaining two divergent Dockerfiles — this keeps the modular-monolith deployment shape of Architecture §2 without duplicating dependency layers. Tag it under both names if your registry conventions expect distinct repositories.

The worker image needs no extra dependencies, but the API image does not need Celery's beat scheduler; keeping one image accepts that small surplus in exchange for guaranteed parity between the two processes (a report written by the API and read by the worker must see identical models and migrations).

### 2.2 Dockerfile pattern (multi-stage)

```dockerfile
# Stage 1 — build wheels (compiles C extensions once, keeps them out of the runtime image)
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install --no-install-recommends -y \
      build-essential libpq-dev libgdal-dev libgeos-dev libproj-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# Stage 2 — runtime
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# GeoDjango loads GDAL/GEOS/PROJ at runtime — the runtime libs, not the -dev headers
RUN apt-get update && apt-get install --no-install-recommends -y \
      libpq5 gdal-bin libgdal32 libgeos-c1v5 libproj25 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app app      # non-root user
WORKDIR /app
COPY --from=deps /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels
COPY . .
RUN python manage.py collectstatic --noinput          # Django admin assets (FR-30/31)
USER app
EXPOSE 8080
# Process selected at run time; the deployment overrides `command`:
#   api    → uvicorn urbenmend.asgi:application --host 0.0.0.0 --port 8080
#   worker → celery -A urbenmend worker -B --loglevel=info
CMD ["uvicorn", "urbenmend.asgi:application", "--host", "0.0.0.0", "--port", "8080"]
```

Key rules:

- **Non-root user** in the final stage (NFR-5).
- **No secrets baked in** — pass via env vars at runtime. In particular `SECRET_KEY` and `DATABASE_URL` are never present at build time, so `collectstatic` must run with settings that do not require them (`django-environ` defaults, or a dedicated build settings module).
- **`python:3.x-slim`, not Alpine.** GeoDjango's native dependency chain (GDAL/GEOS/PROJ) and `psycopg` have no musl wheels; on Alpine every one of them compiles from source, inflating build times and producing a fragile image. The Debian-slim base is larger but reliable — this is a deliberate trade (ADR-001 §4).
- **`libgdal-dev` is a build-stage dependency; the runtime needs only the shared libraries.** If GeoDjango cannot find them, set `GDAL_LIBRARY_PATH` / `GEOS_LIBRARY_PATH` explicitly in settings rather than symlinking inside the image.
- Pin base image tags to a digest or minor version for reproducibility, and pin every line of `requirements.txt` (hash-pinned via `pip-compile`/`uv` is preferred — an unpinned transitive dependency defeats the SHA-tagged deployment model of §2.3).
- `.dockerignore` excludes `.git`, `__pycache__`, `*.pyc`, `.venv`, test fixtures, local env files, and any local media/static output directories.
- ⚠️ Do **not** run `migrate` in the Dockerfile or the container entrypoint. Migrations are a deploy step (§7), not an image or start-up step — otherwise N replicas race each other on rollout.

### 2.3 Image tagging convention

```
<registry>/<image>:<git-sha>          ← immutable, used in deployments
<registry>/<image>:staging            ← mutable, points to latest staging build
<registry>/<image>:latest             ← mutable, points to latest prod release
```

Never deploy `latest` in production — always deploy a specific SHA tag.

---

## 3. Local Development Environment

```
docker-compose.yml          ← all backing services + both app processes
docker-compose.override.yml ← developer-local overrides (hot reload, debug ports)
```

### 3.1 Compose service map

```yaml
services:
  db: # postgis/postgis image — PostgreSQL + PostGIS (NFR-1)
  redis: # Celery broker + cache + session cache (cached_db backend)
  storage: # S3-compatible object store (e.g. MinIO)
  api: # urbenmend image, uvicorn --reload — mounts source for hot reload
  worker: # urbenmend image, celery worker -B — mounts source for hot reload
```

Use the `postgis/postgis` image rather than plain `postgres`; the extension must exist before the first migration enables it (T0.4).

Docker Compose is the **mandated** local environment for this project, not a convenience: GeoDjango requires GDAL/GEOS/PROJ, which are awkward to install natively on Windows — this team's platform (Project Plan §5, P0 stack note).

Celery's beat scheduler runs in-process with the worker locally (`-B`). In production, run beat as a **single** replica separate from the workers; two beat schedulers double-fire the outbox relay (T6.2) and every periodic job.

### 3.2 Environment variables (local)

Store in `.env.local` (git-ignored). A `.env.example` with placeholder values is committed. Read them through `django-environ` in the settings module so the same names work locally and in deployment.

```
DJANGO_SETTINGS_MODULE=urbenmend.settings.local
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

Note the `postgis://` scheme rather than `postgres://` — that is what selects the `django.contrib.gis.db.backends.postgis` engine when the URL is parsed.

---

## 4. CI Pipeline

Run on every push and pull request. All stages must pass before merge to `staging` or `main`.

```
┌─────────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌────────┐
│  lint &     │──▶│  unit    │──▶│ integra-│──▶│  build &    │──▶│ push   │
│  type-check │   │  tests   │   │ tion     │   │  scan image  │   │ image  │
└─────────────┘   └──────────┘   │  tests   │   └──────────────┘   └────────┘
                                 └──────────┘
```

### 4.1 Stage details

| Stage             | What runs                                                                                          | Fails on                                     |
| ----------------- | -------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Lint & type-check | `ruff check` + `ruff format --check`; `mypy` (T0.5)                                                 | Any lint error or type error                 |
| Model drift check | `manage.py makemigrations --check --dry-run`                                                        | A model change without its migration         |
| Deploy check      | `manage.py check --deploy` against production settings                                              | Any Django security-configuration warning    |
| Unit tests        | `pytest` — fast in-process tests, no external deps                                                  | Any test failure                             |
| Integration tests | `pytest` (`pytest-django` + `factory_boy`) against real PostGIS/Redis/storage as CI services        | Any test failure or migration error          |
| Build & scan      | `docker build`; vulnerability scan (e.g. Trivy)                                                     | Build error; critical/high CVE               |
| Push image        | Push SHA-tagged image to registry                                                                   | Registry auth failure                        |

The **model drift check** is cheap and catches the most common Django review miss: a model edited without `makemigrations`, which passes tests locally against an already-migrated dev database and then fails on a fresh deploy.

Integration tests need the `postgis/postgis` service image and a database user permitted to `CREATE EXTENSION` — the test database is built from zero on every run.

⚠️ The T8.1 append-only audit constraint is enforced by **revoking UPDATE/DELETE from the application role** (Project Plan P8). CI's database user therefore cannot be the same role the application uses at runtime, or the grant script itself becomes untestable. Apply the revoke as a migration and assert its effect in an integration test that expects the write to fail.

### 4.2 Migration check

Run `manage.py migrate` against a fresh database in CI and verify it applies cleanly from zero. Then verify reversibility by migrating back down (`manage.py migrate <app> zero` for each app, in dependency order). Fail the pipeline if either direction errors.

⚠️ `RunPython` data migrations are **not** reversible unless a reverse callable is supplied; see §7.

### 4.3 Contract tests

Run API contract tests against the built image (spin up the API container, run tests against `04-api-specification.md` schemas). Fail if any response shape or status code diverges from the spec.

### 4.4 Secrets in CI

Store all secrets (registry credentials, LLM key, DB password, `DJANGO_SECRET_KEY`) in the CI platform's secret store. Inject as environment variables. Never log or echo secret values. Django's `SECRET_KEY` must be **stable across deployments** — rotating it invalidates all signed cookies, session hashes, and signed URLs (including the signed export URLs of NFR-12) — so it is provisioned once per environment, not regenerated per build.

---

## 5. CD — Deployment Pipeline

Triggered automatically after CI passes on `staging` or `main`, or manually for production.

```
CI passes ──▶ deploy to staging (auto) ──▶ smoke tests ──▶ manual gate ──▶ deploy to prod
```

### 5.1 Environments

| Environment | Branch        | Deploy trigger      | Purpose           |
| ----------- | ------------- | ------------------- | ----------------- |
| dev         | feature/\*    | Manual / PR preview | Developer testing |
| staging     | staging       | Auto on merge       | Integration + QA  |
| production  | main (tagged) | Manual approval     | Live traffic      |

### 5.2 Deployment steps (per environment)

1. Pull the SHA-tagged image built by CI.
2. Run database migrations (`manage.py migrate`) as a pre-deploy Job before starting new pods/containers (§7).
3. Perform a rolling update (zero-downtime): bring up new instances, health-check them, then drain old ones.
4. Run smoke tests against the environment.
5. On failure: automatic rollback to the previous SHA tag.

⚠️ The worker deployment must be rolled with the API, not left behind. A worker running the previous image against the new schema will fail on any model field it does not know about — and because Celery retries, those failures surface as a growing queue rather than a failed deploy.

### 5.3 Rollback

Keep the previous deployment's image SHA recorded. To roll back:

1. Re-deploy the previous SHA tag (no rebuild needed).
2. If the migration introduced a schema change, reverse it (`manage.py migrate <app> <previous_migration>`) first — only if the migration is reversible and data loss is acceptable; otherwise fix forward. ⚠️ Reversibility is **not** the default for `RunPython` data migrations (§7), so confirm the reverse path exists before choosing rollback over fix-forward. Backward-compatible migrations (§7) mean most rollbacks need no schema change at all — which is the point of writing them that way.

---

## 6. Kubernetes (K8s)

### 6.1 Workload layout

```
Namespace: urbenmend-<env>
│
├── Deployment: api          (replicas: 2+ in prod; uvicorn)
├── Deployment: worker       (replicas: 1–2; scale on queue depth; celery worker)
├── Deployment: beat         (replicas: EXACTLY 1; celery beat — outbox relay T6.2 + periodic jobs)
├── Job: migrate-<git-sha>   (pre-deploy, run-once; manage.py migrate — §7)
├── Service: api-svc         (ClusterIP → Ingress)
├── Ingress: api-ingress      (TLS termination, routing)
├── ConfigMap: app-config    (non-secret env vars: DJANGO_SETTINGS_MODULE, ALLOWED_HOSTS, …)
├── Secret: app-secrets      (DATABASE_URL, DJANGO_SECRET_KEY, LLM_API_KEY, …)
├── HorizontalPodAutoscaler: api   (scale on CPU/RPS)
├── HorizontalPodAutoscaler: worker (scale on Redis queue depth via KEDA or custom metric)
└── CronJob: (none currently — Celery beat handles periodic work in-process)
```

All four workloads run the **same image** (§2.1), differing only in `command`.

⚠️ **`beat` is a separate Deployment pinned to one replica, and it is never autoscaled.** Two schedulers double-fire every periodic task: the outbox relay would attempt duplicate notification sends (mitigated but not made free by the `skip_locked` claim in T6.2), and any scheduled cleanup or export job would run twice. Set `replicas: 1` with `strategy.type: Recreate`, so a rolling update does not briefly run two.

### 6.2 Deployment manifest pattern

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: urbenmend-prod
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: <registry>/urbenmend/api:<git-sha>
          command:
            ["uvicorn", "urbenmend.asgi:application", "--host", "0.0.0.0", "--port", "8080"]
          ports:
            - containerPort: 8080
          envFrom:
            - configMapRef:
                name: app-config
            - secretRef:
                name: app-secrets
          readinessProbe:
            httpGet:
              path: /api/v1/health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /api/v1/health
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          securityContext:
            runAsNonRoot: true
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
          volumeMounts:
            - name: tmp # required: Django writes chunked uploads + Pillow
              mountPath: /tmp # intermediates here (§9). Mandatory on worker.
      volumes:
        - name: tmp
          emptyDir: {}
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: DoNotSchedule
          labelSelector:
            matchLabels:
              app: api
```

### 6.3 Secrets management

Never store secrets in ConfigMaps or manifests. Options (pick one):

- **K8s Secrets** (base64-encoded, RBAC-restricted) — acceptable for small teams; enable encryption at rest.
- **External secrets operator** — syncs from a secrets manager (Vault, cloud KMS) into K8s Secrets automatically.
- **Sealed Secrets** — encrypt secrets before committing; decrypt only inside the cluster.

### 6.4 Ingress & TLS

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-ingress
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod # or your CA
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  tls:
    - hosts: [api.urbenmend.example.com]
      secretName: api-tls
  rules:
    - host: api.urbenmend.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: api-svc
                port:
                  number: 80
```

TLS must be enforced; HTTP redirects to HTTPS (NFR-5). HSTS header set at the ingress or application layer.

### 6.5 Autoscaling

```yaml
# API — scale on CPU
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
```

Worker scaling: use a queue-depth metric (Redis list length or KEDA `RedisListsScaler`) so workers scale with backlog, not CPU.

### 6.6 Pod Disruption Budget

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: api
```

Ensures at least one API pod stays up during node drains or rolling updates.

---

## 7. Database Migrations

- Migrations are **code** — committed to the repo, reviewed in PRs, never applied manually in production.
- **Django migrations** are the migration tool (ADR-001, T0.4). They are generated with `makemigrations`, but a generated migration is a **draft to be reviewed**, not an artifact to be trusted: check the operation order, and never edit a migration that has already been applied to a shared environment.
- The **first migration enables PostGIS** (`CreateExtension("postgis")`), so it must run against a database whose role can create extensions, and before any migration that declares a geometry column.
- **Migration-on-deploy strategy:** run `manage.py migrate` as a **pre-deploy Job**, not an init container. An init container runs once per pod, so N replicas race each other; a Job runs once per deploy. Django takes a lock per migration, so a race is more likely to stall the rollout than to corrupt the schema — but the Job is the correct shape either way. The old pods continue serving traffic until the new pods pass readiness checks.
- **Backward-compatible migrations only** for zero-downtime deploys: add columns as nullable first, backfill, then add constraints in a later migration. During a rolling update, old and new code run against one schema simultaneously — so a rename is always three deploys (add, dual-write/backfill, drop), never one.
- **Long-lived locks are the real zero-downtime hazard.** Adding an index on a populated table (the GiST indexes of T4.2 especially) locks writes for the duration; use `AddIndexConcurrently` in a non-atomic migration (`atomic = False`) so the deploy does not block report submission. `ALTER TABLE ... ADD CONSTRAINT` and `VALIDATE CONSTRAINT` should likewise be split.
- Keep every migration reversible and test it in CI (§4.2).
  ⚠️ **`RunPython` data migrations are not automatically reversible.** Django refuses to reverse one unless a `reverse_code` callable is supplied, so the "reversible by default" assumption behind the §5.3 rollback path does not hold for data migrations unless the author writes the reverse explicitly. Every `RunPython` operation must therefore either supply a real reverse function, or supply `migrations.RunPython.noop` **with a comment stating why reversal is a no-op** — an unexplained `noop` silently converts a rollback into data loss. Where neither is honest (a destructive backfill), say so in the PR and treat that release as fix-forward only (§5.3).
- ⚠️ `RunPython` receives historical model states, not the current ones. Data migrations must use `apps.get_model(...)` and must not import from application code — including the `services.py` / `selectors.py` layer — or they break the moment that code moves on.

---

## 8. Observability

### 8.1 Structured logging

- All processes emit **structured JSON logs** to stdout/stderr (never to files inside the container). `structlog` provides the formatting, wired through Django's `LOGGING` config so framework and third-party loggers land in the same JSON stream (T0.9).
- Every log line includes: `timestamp`, `level`, `traceId`, `service` (`api` | `worker`), `message`.
- The `traceId` is bound by middleware on the API side and must be **propagated into Celery task headers**, then re-bound in a task prelude — otherwise the worker half of every report submission is uncorrelated with the request that created it, which is precisely the correlation NFR-9 asks for.
- Log aggregator (Loki, ELK, CloudWatch Logs, etc.) collects from container stdout.
- ⚠️ Keep `DEBUG = false` in every deployed environment. Django's debug pages render settings and query parameters, and its SQL query log grows unbounded per request under `DEBUG`.

### 8.2 Metrics

Expose a `/metrics` endpoint (Prometheus format) via `django-prometheus`, or push to your metrics backend. `django-prometheus` supplies request latency, response-code counts, and database/cache instrumentation; Celery metrics come from the worker's own exporter, and the LLM cost counter (NFR-13) is application-defined. Key metrics:

| Metric                         | Alert threshold       |
| ------------------------------ | --------------------- |
| HTTP p99 latency               | > 500 ms (NFR-2)      |
| HTTP error rate (5xx)          | > 1% over 5 min       |
| Worker job queue depth         | > 500 jobs            |
| Worker job processing time p99 | > 30 s                |
| LLM API cost (rolling 24 h)    | > budget cap (NFR-13) |
| DB connection pool saturation  | > 80%                 |
| Redis memory usage             | > 80%                 |
| Outbox relay backlog / oldest unrelayed row age | > 5 min (T6.1/T6.2) |

⚠️ `/metrics` must not be exposed publicly. Keep it off the Ingress path (§6.4) and scrape it on the pod port, or the operational picture of the deployment is readable by anyone.

The **outbox backlog** row is a Django-stack addition rather than a new requirement: because the relay is a Celery beat job polling a table (T6.2), a stalled beat scheduler is silent — no queue depth grows, no errors are logged, notifications simply stop. Age of the oldest unrelayed row is the only signal that distinguishes "nothing to send" from "relay is dead".

### 8.3 Distributed tracing

Propagate a `traceId` (API §4.1) through all service calls. Use the OpenTelemetry Django and Celery instrumentations so the span context crosses the enqueue boundary rather than restarting at the worker. Export spans to a tracing backend (Jaeger, Zipkin, OTLP-compatible). Correlate logs and traces by `traceId`.

### 8.4 Health checks

`GET /api/v1/health` returns dependency degradation flags (Architecture §12, API §6.16). K8s readiness probe uses this endpoint — a degraded dependency (e.g. Redis down) marks the pod not-ready and stops traffic routing to it.

---

## 9. Security Hardening

| Control                 | Implementation                                                                                                                    |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Non-root container      | `runAsNonRoot: true` in pod security context                                                                                      |
| Read-only filesystem    | `readOnlyRootFilesystem: true`; mount writable volumes only where needed (tmp)                                                    |
| No privilege escalation | `allowPrivilegeEscalation: false`                                                                                                 |
| Django security checks  | `manage.py check --deploy` in CI (§4.1); `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE`, `CSRF_COOKIE_SECURE` per API spec §2 |
| Network policies        | Restrict pod-to-pod traffic: only `api` and `worker` may reach `db` and `redis`; `db` and `redis` accept no external traffic      |
| Image scanning          | Scan on every CI build; block deploy on critical/high CVEs                                                                        |
| Secrets rotation        | Rotate DB passwords, **session secrets and `DJANGO_SECRET_KEY` on a schedule**; use external secrets operator to propagate without redeployment |
| TLS everywhere          | HTTPS enforced at ingress; internal service-to-service traffic over TLS where supported                                           |
| Rate limiting           | Enforced at application layer (DRF throttling, NFR-13); optionally also at ingress for coarse protection                          |
| RBAC (K8s)              | Service accounts with least-privilege; no default service account tokens mounted                                                  |

Django-specific notes:

- ⚠️ **`readOnlyRootFilesystem: true` breaks Django file uploads.** The media upload path (T2.4) writes to storage, so the main API is safe — but Django writes to `/tmp` for chunked uploads and for Pillow's orient/compress pipeline (T2.5) before the finished file reaches S3. The worker pod needs an **emptyDir mount at `/tmp`**; without it, image uploads fail at runtime with read-only-filesystem errors that look nothing like upload bugs. The API pod should mount it too, defensively, and `TMPDIR` may be pointed at the mounted volume.
- **`/metrics` is not a Django app route and must not appear on the Ingress** (§8.2).
- **Session revocation is a database op.** Because sessions use the `cached_db` backend (BR-25/BR-33, ADR-001 §2), the sessions table carries cache-lookup fallback for sessions evicted from Redis. Backing up Redis alone is therefore not a session-backup story; the `django_session` rows in Postgres are the source of truth, and truncating that table on rollback (§5.3) is what logs everyone out — which may be desirable after an incident and undesirable during a routine rollback. Decide this per incident, not by habit.

---

## 10. Backup & Restore

| Asset                | Backup method                                | Frequency                       | Retention | Restore drill              |
| -------------------- | -------------------------------------------- | ------------------------------- | --------- | -------------------------- |
| PostgreSQL           | `pg_dump` or continuous WAL archiving        | Daily snapshot + continuous WAL | 30 days   | Monthly restore to staging |
| Object store (media) | Cross-region replication or versioned bucket | Continuous                      | 90 days   | Quarterly restore drill    |
| Redis                | RDB snapshot (persistence enabled)           | Hourly                          | 7 days    | On-demand                  |

Restore procedure must be documented and rehearsed before go-live (NFR-10, §9 checklist).

---

## 11. Runbook Skeleton

Each operational event should have a runbook entry. Minimum set:

| Event                        | Runbook                                                                                                                                     |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Deploy to production         | Pull SHA tag → run `manage.py migrate` pre-deploy Job → rolling update → smoke test → verify metrics (§5.2) |
| Rollback                     | Re-deploy previous SHA tag → reverse schema only if the migration is reversible (§5.3, §7) → verify health → investigate root cause |
| LLM outage                   | Keyword fallback activates automatically (RISK-3); monitor classification source metric; no manual action needed unless fallback also fails |
| Worker crash / queue backlog | Check pod logs; restart pod; monitor queue depth draining. Check beat is alive (outbox backlog metric, §8.2) before assuming the queue is the whole story |
| DB failover                  | Promote replica; update `DATABASE_URL` secret; rolling restart of api + worker + beat |
| High error rate              | Check logs for `traceId` patterns; check DB/Redis health; check LLM cost cap |
| Security incident            | Revoke sessions server-side (BR-25/33) by deleting `django_session` rows; rotate secrets incl. `DJANGO_SECRET_KEY`; audit log review |

---

## 12. Self-Review

### Are all NFRs covered?

- ✅ NFR-1/2/3 (latency/throughput): HPA, PostGIS/GiST indexes (added concurrently, §7), load test gate in P10.
- ✅ NFR-4 (availability/degradation): health checks, readiness probes, LLM fallback, outbox replay + backlog-age alert (§8.2).
- ✅ NFR-5 (security): non-root containers, TLS, secrets management, network policies, image scanning, `check --deploy` in CI.
- ✅ NFR-9 (CI/CD, observability): full CI pipeline incl. model-drift gate, structured logs, metrics, tracing across the enqueue boundary.
- ✅ NFR-10 (audit/backup): append-only audit log enforced by DB grant (P8), backup schedule, restore drill.
- ✅ NFR-12/13 (export, cost caps): signed export URLs, LLM cost metric + alert, DRF throttling.

### Open items

- LLM provider is deferred (Q9 RESOLVED: adapter stays provider-agnostic) — plug in credentials when provider is chosen; no pipeline change needed.
- Specific registry, ingress controller, and secrets backend are left as operator choices — patterns above apply to any equivalent.
- The Python version is written as `python:3.12-slim` in §2.2 as a concrete example; pin whichever version the team standardises on in T0.1, and confirm GeoDjango supports it before pinning.

### Changed in v1.1 (ADR-001)

The framework was open when v1.0 was written, so its container examples defaulted to a Node-shaped pattern. v1.1 retargets them to Django + DRF. No NFR, deployment topology, or environment model changed; the additions are Django operational hazards that would otherwise be discovered in production:

- Single image, process chosen by `command` (§2.1, §6.1) — replaces "two images extend a common base".
- Debian-slim over Alpine, with the GDAL/GEOS build/runtime split (§2.2).
- **Celery beat as a single-replica Deployment** (§6.1) — duplicate schedulers double-fire the outbox relay.
- **`readOnlyRootFilesystem` needs a writable `/tmp`** for uploads and Pillow (§6.2, §9).
- **`RunPython` is not auto-reversible** (§7) — v1.0's "keep a `migrate down` for every migration" assumed otherwise.
- Concurrent index creation for the T4.2 GiST indexes (§7).
- Model-drift and `check --deploy` CI gates (§4.1); outbox backlog metric (§8.2).

---

_End of `docs/06-devops-guide.md` (v1.1). Platform-agnostic and targeted at the Django + DRF stack committed in ADR-001; all patterns trace to approved planning docs 01–05 and 07._
