"""
Cross-cutting API infrastructure (A8, T0.6).

The three contracts in `docs/04-api-specification.md` that DRF does **not** supply by default
live here, so there is one implementation each rather than one per view:

- `exceptions.py` — the `{error: {code, message, details, traceId}}` envelope (API §4.1).
- `pagination.py` — the `{data, page, meta}` envelope with opaque cursors (API §1.3, §4.4).
- `urls.py` — the `/api/v1` router (API §5).

⚠️ This package holds **no domain logic**. Business rules and authorization stay in each app's
`services.py`/`selectors.py` [doc: Arch §2.4, R-12/DC-3].
"""
