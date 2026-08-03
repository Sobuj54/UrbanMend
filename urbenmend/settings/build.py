"""
Build-time settings (A4, T0.3) — used ONLY by the Dockerfile's `collectstatic` step.

No secrets exist at build time [doc: DevOps §2.2], so `SECRET_KEY` and `DATABASE_URL` —
both required with no fallback in base.py — are injected here as throwaway values BEFORE
base is imported. Nothing here is ever used at runtime: the api and worker containers set
`DJANGO_SETTINGS_MODULE=urbenmend.settings.prod` (or `.dev` locally).

⚠️ Do not import this module from dev.py or prod.py, and do not add real config to it.
Its only job is to let `manage.py collectstatic` construct the app registry offline.

`DATABASE_URL` still uses the `postgis://` scheme — base.py asserts the resolved engine is
the GeoDjango one, and that assertion should hold here too. No connection is opened;
collectstatic touches no table.
"""

import os

# Must precede the base import — base.py reads both at module level.
os.environ.setdefault("DJANGO_SECRET_KEY", "build-only-not-a-secret-never-used-at-runtime")
os.environ.setdefault("DJANGO_DEBUG", "false")
os.environ.setdefault("DATABASE_URL", "postgis://build:build@127.0.0.1:5432/build")

from .base import *  # noqa: E402, F401, F403
