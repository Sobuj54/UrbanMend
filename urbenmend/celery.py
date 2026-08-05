"""
Celery application (A8, T0.7).

One app object, shared by the API and the worker. Same image, different entrypoint
[doc: Arch §2.2, Plan T0.7] — the worker runs `celery -A urbenmend worker -B` while the API
imports this module only so `shared_task` binds to a configured app.

⚠️ Settings come from Django via `config_from_object(..., namespace="CELERY")`, so every
`CELERY_*` name in `settings/base.py` is the single source of truth. Do not configure the
broker here — a second source would let the API and the worker disagree about which Redis
database holds the queue (cache is db 0, broker is db 1).
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from celery.signals import setup_logging

# Must precede the Celery() construction: `config_from_object("django.conf:settings")`
# resolves settings eagerly, and an unset DJANGO_SETTINGS_MODULE fails there rather than
# at first task execution. `setdefault` so the compose/K8s value always wins.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbenmend.settings.dev")

app = Celery("urbenmend")

# `namespace="CELERY"` maps CELERY_BROKER_URL → broker_url, etc. Keeping the prefix means
# `manage.py diffsettings` shows queue config alongside the rest, instead of bare lowercase
# names that read like typos.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discovers `tasks.py` in every entry of INSTALLED_APPS. Apps nest under `urbenmend.`, and
# autodiscovery follows the INSTALLED_APPS strings, so the nesting needs no special handling.
app.autodiscover_tasks()


# ⚠️ Imported for its import side effect, not for a name. `celery_tracing`'s signal receivers
# register at decoration time, so a module nobody imports connects nothing — the enqueue-boundary
# propagation (T0.9) would silently no-op while its unit tests still passed, since they import the
# module directly. Both processes reach this line through `urbenmend/__init__.py`, and the API side
# needs it as much as the worker: the API is the publisher that stamps the id.
#
# Safe this early: the chain pulls in `uuid`, `contextvars` and `structlog` only — no Django
# models, and no settings access at import time.
from urbenmend.platform import celery_tracing  # noqa: E402,F401  (side-effecting import)


@setup_logging.connect
def _configure_celery_logging(**_kwargs: Any) -> None:
    """Stop Celery from replacing Django's logging configuration.

    Celery hijacks the root logger on worker start by default, which would discard the
    structlog JSON formatter (T0.9) and emit plain-text records instead — so worker output
    would not be machine-parseable while API output was [doc: DevOps §8.2]. An empty receiver
    is the documented way to suppress that: connecting to the signal at all is what tells
    Celery the application owns logging.
    """
    return None
