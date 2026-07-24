# UrbanMend — DevOps Guide

> Platform- and stack-agnostic guide covering containerisation, CI/CD, orchestration, and deployment for the UrbanMend backend (API process + Worker process).

|                     |                                                                                               |
| ------------------- | --------------------------------------------------------------------------------------------- |
| **Document**        | `docs/06-devops-guide.md`                                                                     |
| **Version**         | 1.0                                                                                           |
| **Status**          | Planning phase                                                                                |
| **Author role**     | DevOps / Platform Engineer                                                                    |
| **Date**            | 2026-07-24                                                                                    |
| **Source of truth** | `02-architecture.md` · `04-api-specification.md` · `05-project-plan.md`                       |
| **Scope**           | Backend only (API process + Worker process + supporting services). Client/UI is out of scope. |

### Ground rules

- All tooling choices below are **illustrative patterns**, not mandates. Swap any named tool for an equivalent that fits your platform.
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
urbenmend/api     ← HTTP API process  (Architecture §2.1)
urbenmend/worker  ← Async job worker  (Architecture §2.2)
```

Shared application code lives in a common library layer; both images extend a common base.

### 2.2 Dockerfile pattern (multi-stage)

```dockerfile
# Stage 1 — deps
FROM <runtime>:<version>-alpine AS deps
WORKDIR /app
COPY package*.json ./          # or requirements.txt, pom.xml, go.mod, etc.
RUN <package-manager> install --production

# Stage 2 — build (compiled languages only; skip for interpreted)
FROM deps AS build
COPY . .
RUN <build-command>

# Stage 3 — runtime
FROM <runtime>:<version>-alpine AS runtime
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app   # non-root user
COPY --from=build /app/dist ./dist             # or from deps for interpreted
COPY --from=deps /app/node_modules ./node_modules
USER app
EXPOSE 8080
ENTRYPOINT ["<start-command>"]
```

Key rules:

- **Non-root user** in the final stage (NFR-5).
- **No secrets baked in** — pass via env vars at runtime.
- Pin base image tags to a digest or minor version for reproducibility.
- `.dockerignore` excludes `.git`, test files, local env files, and build artifacts.

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
  db: # PostgreSQL + PostGIS
  redis: # Job queue + cache
  storage: # S3-compatible object store (e.g. MinIO)
  api: # urbenmend/api — mounts source for hot reload
  worker: # urbenmend/worker — mounts source for hot reload
```

### 3.2 Environment variables (local)

Store in `.env.local` (git-ignored). A `.env.example` with placeholder values is committed.

```
DATABASE_URL=postgres://user:pass@db:5432/urbenmend
REDIS_URL=redis://redis:6379
STORAGE_ENDPOINT=http://storage:9000
STORAGE_BUCKET=urbenmend-media
SESSION_SECRET=<local-dev-secret>
LLM_API_KEY=<your-key>
```

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

| Stage             | What runs                                                    | Fails on                            |
| ----------------- | ------------------------------------------------------------ | ----------------------------------- |
| Lint & type-check | Linter, formatter check, static analysis                     | Any lint error or type error        |
| Unit tests        | Fast in-process tests, no external deps                      | Any test failure                    |
| Integration tests | Tests against real DB/Redis/storage (spun up as CI services) | Any test failure or migration error |
| Build & scan      | `docker build` both images; vulnerability scan (e.g. Trivy)  | Build error; critical/high CVE      |
| Push image        | Push SHA-tagged images to registry                           | Registry auth failure               |

### 4.2 Migration check

Run `migrate up` against a fresh DB in CI and verify it applies cleanly from zero. Run `migrate down` to verify reversibility. Fail the pipeline if either direction errors.

### 4.3 Contract tests

Run API contract tests against the built image (spin up the API container, run tests against `04-api-specification.md` schemas). Fail if any response shape or status code diverges from the spec.

### 4.4 Secrets in CI

Store all secrets (registry credentials, LLM key, DB password) in the CI platform's secret store. Inject as environment variables. Never log or echo secret values.

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
2. Run database migrations (`migrate up`) before starting new pods/containers.
3. Perform a rolling update (zero-downtime): bring up new instances, health-check them, then drain old ones.
4. Run smoke tests against the environment.
5. On failure: automatic rollback to the previous SHA tag.

### 5.3 Rollback

Keep the previous deployment's image SHA recorded. To roll back:

1. Re-deploy the previous SHA tag (no rebuild needed).
2. If the migration introduced a schema change, run `migrate down` first — only if the migration is reversible and data loss is acceptable; otherwise fix forward.

---

## 6. Kubernetes (K8s)

### 6.1 Workload layout

```
Namespace: urbenmend-<env>
│
├── Deployment: api          (replicas: 2+ in prod)
├── Deployment: worker       (replicas: 1–2; scale on queue depth)
├── Service: api-svc         (ClusterIP → Ingress)
├── Ingress: api-ingress      (TLS termination, routing)
├── ConfigMap: app-config    (non-secret env vars)
├── Secret: app-secrets      (DATABASE_URL, SESSION_SECRET, LLM_API_KEY, …)
├── HorizontalPodAutoscaler: api   (scale on CPU/RPS)
├── HorizontalPodAutoscaler: worker (scale on Redis queue depth via KEDA or custom metric)
└── CronJob: (none currently — worker handles async jobs via queue)
```

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
- Use a migration tool appropriate to your stack (Flyway, Liquibase, Alembic, golang-migrate, etc.).
- **Migration-on-deploy strategy:** run `migrate up` as an init container or a pre-deploy job before the new pods start. The old pods continue serving traffic until the new pods pass readiness checks.
- **Backward-compatible migrations only** for zero-downtime deploys: add columns as nullable first, backfill, then add constraints in a later migration.
- Keep a `migrate down` script for every migration; test it in CI.

---

## 8. Observability

### 8.1 Structured logging

- All processes emit **structured JSON logs** to stdout/stderr (never to files inside the container).
- Every log line includes: `timestamp`, `level`, `traceId`, `service` (`api` | `worker`), `message`.
- Log aggregator (Loki, ELK, CloudWatch Logs, etc.) collects from container stdout.

### 8.2 Metrics

Expose a `/metrics` endpoint (Prometheus format) or push to your metrics backend. Key metrics:

| Metric                         | Alert threshold       |
| ------------------------------ | --------------------- |
| HTTP p99 latency               | > 500 ms (NFR-2)      |
| HTTP error rate (5xx)          | > 1% over 5 min       |
| Worker job queue depth         | > 500 jobs            |
| Worker job processing time p99 | > 30 s                |
| LLM API cost (rolling 24 h)    | > budget cap (NFR-13) |
| DB connection pool saturation  | > 80%                 |
| Redis memory usage             | > 80%                 |

### 8.3 Distributed tracing

Propagate a `traceId` (API §4.1) through all service calls. Export spans to a tracing backend (Jaeger, Zipkin, OTLP-compatible). Correlate logs and traces by `traceId`.

### 8.4 Health checks

`GET /api/v1/health` returns dependency degradation flags (Architecture §12, API §6.16). K8s readiness probe uses this endpoint — a degraded dependency (e.g. Redis down) marks the pod not-ready and stops traffic routing to it.

---

## 9. Security Hardening

| Control                 | Implementation                                                                                                                    |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Non-root container      | `runAsNonRoot: true` in pod security context                                                                                      |
| Read-only filesystem    | `readOnlyRootFilesystem: true`; mount writable volumes only where needed (tmp)                                                    |
| No privilege escalation | `allowPrivilegeEscalation: false`                                                                                                 |
| Network policies        | Restrict pod-to-pod traffic: only `api` and `worker` may reach `db` and `redis`; `db` and `redis` accept no external traffic      |
| Image scanning          | Scan on every CI build; block deploy on critical/high CVEs                                                                        |
| Secrets rotation        | Rotate DB passwords, session secrets, and LLM keys on a schedule; use external secrets operator to propagate without redeployment |
| TLS everywhere          | HTTPS enforced at ingress; internal service-to-service traffic over TLS where supported                                           |
| Rate limiting           | Enforced at application layer (NFR-13); optionally also at ingress for coarse protection                                          |
| RBAC (K8s)              | Service accounts with least-privilege; no default service account tokens mounted                                                  |

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
| Deploy to production         | Pull SHA tag → run migrations → rolling update → smoke test → verify metrics                                                                |
| Rollback                     | Re-deploy previous SHA tag → verify health → investigate root cause                                                                         |
| LLM outage                   | Keyword fallback activates automatically (RISK-3); monitor classification source metric; no manual action needed unless fallback also fails |
| Worker crash / queue backlog | Check pod logs; restart pod; monitor queue depth draining                                                                                   |
| DB failover                  | Promote replica; update `DATABASE_URL` secret; rolling restart of api + worker                                                              |
| High error rate              | Check logs for `traceId` patterns; check DB/Redis health; check LLM cost cap                                                                |
| Security incident            | Revoke sessions server-side (BR-25/33); rotate secrets; audit log review                                                                    |

---

## 12. Self-Review

### Are all NFRs covered?

- ✅ NFR-1/2/3 (latency/throughput): HPA, PostGIS indexes, load test gate in P10.
- ✅ NFR-4 (availability/degradation): health checks, readiness probes, LLM fallback, outbox replay.
- ✅ NFR-5 (security): non-root containers, TLS, secrets management, network policies, image scanning.
- ✅ NFR-9 (CI/CD, observability): full CI pipeline, structured logs, metrics, tracing.
- ✅ NFR-10 (audit/backup): append-only audit log (P8), backup schedule, restore drill.
- ✅ NFR-12/13 (export, cost caps): signed export URLs, LLM cost metric + alert.

### Open items

- LLM provider is deferred (Q9 RESOLVED: adapter stays provider-agnostic) — plug in credentials when provider is chosen; no pipeline change needed.
- Specific registry, ingress controller, and secrets backend are left as operator choices — patterns above apply to any equivalent.

---

_End of `docs/06-devops-guide.md` (v1.0). Platform- and stack-agnostic; all patterns trace to approved planning docs 01–05._
