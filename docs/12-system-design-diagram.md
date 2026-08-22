# UrbanMend system design diagram

This diagram describes the system currently implemented in this repository. It distinguishes the
local Docker Compose shape from the production shape documented for Kubernetes/Azure deployment.

```mermaid
flowchart TB
    citizen[Citizen web/mobile client]
    authority[Authority/Admin client]
    ingress[HTTPS ingress / Caddy\nTLS, HSTS, public routing]

    subgraph app[UrbanMend application image]
        api[ASGI API\nDjango + DRF + Uvicorn\n/api/v1, admin, OpenAPI]
        modules[In-process bounded contexts\nIdentity & Access\nReporting + Media\nClassification\nIssues & Clustering\nGeospatial\nDashboard/Query\nNotifications\nModeration, Audit, Export]
        worker[Celery worker\ntriage, media, notifications, relay]
        beat[Celery beat\nperiodic jobs and outbox relay\nproduction: exactly one replica]
    end

    postgres[(PostgreSQL + PostGIS\napplication data, sessions, outbox, audit\nspatial indexes and queries)]
    redis[(Redis\nCelery broker, cache, rate limits\nsession cache)]
    minio[(MinIO / S3-compatible object storage\nmedia objects)]
    llm[Optional LLM provider\nclassification adapter]
    geocoder[Optional geocoder\nreverse-geocoding adapter]
    smtp[SMTP email provider]
    metrics[Private pod-port scrape\n/metrics Prometheus format]
    monitor[Azure Monitor / log and alert backend]

    citizen -->|HTTPS| ingress
    authority -->|HTTPS| ingress
    ingress --> api
    api --> modules
    modules --> postgres
    modules --> redis
    modules --> minio
    modules -->|on-commit task enqueue| redis
    redis --> worker
    beat -->|poll outbox / schedule| redis
    beat --> postgres
    worker --> postgres
    worker --> redis
    worker --> minio
    worker --> llm
    worker --> geocoder
    worker --> smtp
    api -. structured JSON logs .-> monitor
    worker -. structured JSON logs .-> monitor
    beat -. structured JSON logs .-> monitor
    api -. private scrape .-> metrics
    worker -. worker metrics .-> metrics
    metrics -. alerts/dashboards .-> monitor

    classDef data fill:#e8f1ff,stroke:#356ae6,color:#102a5c
    classDef external fill:#fff2cc,stroke:#b8860b,color:#5c4300
    class postgres,redis,minio data
    class llm,geocoder,smtp,monitor external
```

## Request and asynchronous processing boundaries

- API requests are authenticated and authorized at the service layer. State-changing work is
  committed to PostgreSQL before Celery tasks are published with `transaction.on_commit`.
- Report triage is asynchronous: classification runs first, using the LLM adapter when available
  and the deterministic keyword fallback when it is unavailable or over a cost/rate limit; issue
  clustering and proximity context follow.
- Domain events are stored in the PostgreSQL transactional outbox. Celery beat relays pending rows;
  notification consumers are idempotent, so broker redelivery is safe.
- Media processing is asynchronous and stores final objects in S3-compatible storage. The API does
  not use the object store as the source of truth for relational state.
- Trace IDs are bound on API requests, propagated in Celery task headers, and included in structured
  logs and error responses. Query strings are deliberately excluded from logs.

## Runtime topology

### Local Docker Compose

Compose runs `db` (PostgreSQL/PostGIS), `redis`, `storage` (MinIO), `api`, and `worker`. The local
worker uses Celery `-B`, so beat runs in-process for development convenience.

### Production deployment

Production uses the same image with separate workloads: API (ASGI/Uvicorn), worker (Celery), and a
single-replica beat deployment. A SHA-pinned migration Job runs before workload rollout. Ingress
terminates TLS and routes public traffic to the API; `/metrics` is intentionally kept off the public
Ingress and scraped only from the pod port.
