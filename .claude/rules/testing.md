---
description: pytest layout, integration DB requirements, contract tests, CI gates
paths:
  - "**/test_*.py"
  - "**/*_test.py"
  - "**/tests/**"
  - "**/factories.py"
  - "**/conftest.py"
---

# Testing

Sources: `docs/06-devops-guide.md` §4, `docs/05-project-plan.md` §"Standing test requirements",
`docs/07-adr-001-app-framework.md` §1.

Tooling: **`pytest-django` + `factory_boy`**. Every phase carries unit + integration tests in CI, plus
contract tests against the API spec; the regression suite grows each phase.

## Three layers

| Layer | What it is |
|---|---|
| Unit | `pytest` — fast, in-process, **no external deps** |
| Integration | `pytest` against **real PostGIS/Redis/storage** as CI services |
| Contract | responses must match `docs/04-api-specification.md` schemas and status codes |

Contract tests run against the **built image**: spin up the API container and assert every response
shape and status code. Fail on any divergence from the spec.

## Integration test requirements

- Needs the **`postgis/postgis`** service image and a database user permitted to `CREATE EXTENSION` —
  the test database is built from zero on every run.
- ⚠️ The CI database user **cannot be the same role the application uses at runtime**. The T8.1
  append-only constraint is enforced by revoking `UPDATE`/`DELETE` from the *application* role; if CI
  used that same role, the grant script itself would be untestable.
- Assert the revoke's effect directly: an integration test that expects the write to **fail**.

## Migration checks (CI gates, not optional)

- `python manage.py makemigrations --check --dry-run` — catches a model edited without its migration,
  which passes locally against an already-migrated dev DB and then fails on a fresh deploy.
- `python manage.py migrate` against a **fresh** database — must apply cleanly from zero.
- Reversibility: migrate back down with `python manage.py migrate <app> zero` for each app in
  dependency order. Fail the pipeline if either direction errors.
- `python manage.py check --deploy` against production settings — fail on any Django security warning.

## Full CI order

```
lint & type-check → model drift → deploy check → unit → integration → build & scan → push image
```

Runs on every push and pull request. All stages must pass before merge to `staging` or `main`.

## What to cover

Beyond happy paths, the docs call out: concurrency (clustering find-or-create under parallel
submissions), the append-only revoke, invalid status transitions (BR-16), duplicate confirmation
(BR-23), idempotency replay (BR-5), out-of-city rejection (C-11), authority scope leakage returning
403/404, the LLM fallback path (never blocks intake), and the SMS severity gate not being
preference-bypassable.

**Not specified — don't invent and don't claim doc backing:** test directory layout, the
unit-vs-integration marker split (both stages are literally `pytest`), the contract-test library,
coverage tool or threshold, and the load-test tool for T10.4.
