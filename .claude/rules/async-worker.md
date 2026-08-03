---
description: Celery tasks, enqueue discipline, outbox relay, LLM triage fallback
paths:
  - "**/tasks.py"
  - "**/celery.py"
  - "platform/**"
  - "classification/**"
---

# Async worker

Sources: `docs/02-architecture.md` §2.4/§4/§7.1, `docs/05-project-plan.md` P2/P3, `docs/06-devops-guide.md` §3.1/§6.1.

Celery (Redis broker) + Celery beat, sharing one codebase and one image with the API; the process is
selected at run time via the container command.

## Enqueue discipline

**Task enqueue always fires via `transaction.on_commit`** so a worker can never observe an
uncommitted Report (T2.2, Arch §4.1). This is not optional — an eagerly-enqueued task races the
transaction and the worker reads a row that does not exist yet.

## Outbox

The outbox (Arch §7.1) is a **real table** written inside the state-change transaction, polled by a
Celery **beat** relay using row-level `SKIP LOCKED` reads. An in-process commit hook alone cannot
survive the crash this pattern exists to guard against (T6.1).

⚠️ **beat runs as exactly one replica** — a separate Deployment, `replicas: 1`,
`strategy.type: Recreate`, **never autoscaled**. Two beat schedulers double-fire the outbox relay.
Locally, run beat as a single replica separate from the workers.

## Clustering

Find-or-create for Issues takes a **transaction-scoped Postgres advisory lock keyed on
geohash-cell + category** inside an atomic block (Arch §4.3).

Issues are formed **only** by this async clustering path — there is deliberately no `POST /issues`.
Two genuinely different issues at the same coordinates must not wrongly merge. Clustering rule
changes affect **future** clustering only.

## LLM triage

- The provider is **deferred** (Q9 resolved as "deferred"); the adapter stays provider-agnostic —
  a plain ABC with **no Django imports** (T3.1).
- The product **never hard-depends on the external API** (NFR-4).
- On timeout or malformed output, **fall back to keyword rules** (FR-13a). The fallback **never
  blocks intake** and **never blocks the queue** (T3.4, O-2).
- Exceeding LLM cost/rate caps does not fail submission — triage degrades to the keyword fallback and
  the API still returns `202`.
- **Avoid sending unnecessary PII in the prompt**, and prefer a provider/config that does not train on
  submitted data (P7).
- Severity must be **explainable** — no black-box ranking (FR-15). Retain the rationale.
- Triage may **reorder** work; it must never **hide** a reported hazard. Low severity ≠ invisible (T6).

## Notifications

- SMS is **reserved for High severity, gated server-side** regardless of user preference (BR-30,
  T6.6/T6.7). Disabling SMS is honoured; enabling it does **not** bypass the severity gate, and the
  gate is not preference-bypassable (R-8).
- Never send to an unverified/invalid channel, and don't leak the report's existence.
- Debounce notifications on rapid repeated status changes — don't spam.
- ⚠️ The SSE notification stream requires the **ASGI stack**; under WSGI each open stream pins a
  worker thread (T6.8).

## Deploy coupling

⚠️ **The worker deployment must be rolled with the API**, not left behind (DevOps §5.2).

⚠️ `readOnlyRootFilesystem: true` breaks Django file uploads — the worker pod needs an `emptyDir` at
`/tmp` (mandatory on worker). `TMPDIR` may be pointed at the mounted volume.
