---
description: Models, GeoDjango, migration policy, append-only and delete rules
paths:
  - "**/models.py"
  - "**/migrations/**"
  - "**/selectors.py"
  - "**/services.py"
  - "**/admin.py"
---

# Database & models

Sources: `docs/02-architecture.md` §2.4/§9, `docs/06-devops-guide.md` §7, `docs/03-data-model.md`.

Note: `03-data-model.md` is **domain only** — "No schema, columns, keys, indexes, or SQL". Table and
column naming conventions, PK type at DB level, indexing rules, and `updatedAt` conventions are
**not specified**. Don't present invented choices as doc-derived.

## Layering

Business rules, RBAC checks, and transactions live in **`services.py` (writes)** and
**`selectors.py` (reads)**, one pair per app. The data-access layer holds no business logic. DRF views
stay thin.

## GeoDjango

- Report and POI locations are PostGIS `geography(Point, 4326)` — `PointField(geography=True,
  srid=4326)`.
- **GiST spatial indexes on every queried geometry column.**
- Use the `postgis/postgis` image, not plain `postgres`; the extension must exist before the first
  migration enables it (T0.4).
- `DATABASE_URL` uses the **`postgis://`** scheme — that is what selects
  `django.contrib.gis.db.backends.postgis` when the URL is parsed.
- Spatial work (`ST_DWithin`, KNN `<->`, grid aggregation, point-in-polygon) goes through the ORM.

## Migration policy

- **Migrations are code**: committed, reviewed in PRs, **never applied manually in production**.
- ⚠️ **Never run `migrate` in the Dockerfile or the container entrypoint.** It runs as a pre-deploy
  Job.
- **Never edit a migration already applied to a shared environment.**
- **Backward-compatible migrations only** for zero-downtime deploys. A rename is always three
  deploys (add → dual-write/backfill → drop), never one.
- ⚠️ `RunPython` data migrations are **not** reversible unless you supply a reverse callable.
- Data migrations **must** use `apps.get_model(...)` and **must not** import from application code —
  including `services.py`/`selectors.py`.
- The **custom user model must be declared before the first migration** — irreversible (T0.10).

## Append-only histories

Status Events and Audit Events are **never** updatable or deletable by anyone (BR-31, C-9).

⚠️ Enforce this **at the database level** by revoking `UPDATE`/`DELETE` from the application role
(T8.1, NFR-10). Application discipline alone does not satisfy the requirement. Apply the revoke as a
migration and assert its effect in an integration test that expects the write to fail. The CI database
user therefore cannot be the same role the application uses at runtime.

## Deletion

- **No hard deletes.** Categories, POIs, and Severity Keywords use `Active → Retired` lifecycles.
- **C-14** — deleting a user must not orphan or destroy public Issue history; it **anonymizes**.
- Issues are never hard-deleted; moderation hides content (FR-31).

## Domain invariants that shape the schema

- A **Report** is one submission; an **Issue** is a cluster of Reports. Severity, status, and
  assignment live on the Issue. **The Report never carries resolution status.**
- Severity is a bounded set `{Critical, High, Medium, Low}` (C-1, Q2 resolved). Note `03-data-model.md`
  §3 and BR-8 still show a stale 3-band set — the 4-band enum is authoritative.
- **No computed numeric score and no tunable weights** (FR-21 removed).
- Category values come from a controlled taxonomy; no free-form categories (C-2).
- POI/proximity associations are derived and **display-only** — never an input to severity or
  ordering (C-10).
- An Authority severity override **never overwrites** the computed severity; both are retained.
- **BR-4** — a Report's authoritative location is the explicit report coordinate, never photo EXIF;
  EXIF/GPS is stripped by default.
- **C-11** — a location outside the served city boundary is not accepted (`422 OUT_OF_CITY`).
- Taxonomy, POI data, and keyword lists are **data/config, not hard-coded** (NFR-11).
- Single-city only, but the model should not actively prevent a future `city` column (PRD §11).
