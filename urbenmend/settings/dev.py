"""
Development settings (A4, T0.3) — LOCAL ONLY, never deployed.

`DJANGO_SETTINGS_MODULE=urbenmend.settings.dev` is the manage.py fallback and what
docker-compose injects from `.env.local`.
"""

import os
from pathlib import Path

import environ

# Load .env.local for native `python manage.py ...` runs on the host. Inside Compose the
# same variables already arrive via `env_file`, and read_env uses setdefault semantics —
# it never overrides what Compose injected. Must precede the base import, which reads
# DJANGO_SECRET_KEY and DATABASE_URL at module level.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env.local"
if _ENV_FILE.exists():
    environ.Env.read_env(str(_ENV_FILE))

# Local-only fallbacks for the two variables base.py requires with no default. They exist
# so `pytest`, `mypy`, `ruff` and `manage.py check` run on a fresh clone with no .env.local
# — a contributor should not have to provision a secret to lint the code.
#
# ⚠️ Safe ONLY because this module is never deployed: prod.py deliberately has no fallback
# for either, so a deployment with a missing secret fails to boot instead of running on a
# publicly-known key. Do not copy this pattern into prod.py.
# Values match docker-compose.yml's db service so they are correct inside Compose too.
os.environ.setdefault("DJANGO_SECRET_KEY", "dev-only-insecure-not-for-deployment")
os.environ.setdefault("DATABASE_URL", "postgis://urbenmend:urbenmend@db:5432/urbenmend")

from .base import *  # noqa: E402, F401, F403
from .base import LOGGING, env  # noqa: E402  (explicit: mypy/ruff can't see star-imports)

DEBUG = env.bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "api"])

# --------------------------------------------------------------------------------------
# Cookies — no TLS locally
# --------------------------------------------------------------------------------------
# Compose serves plain HTTP on :8080. `Secure` cookies would never be sent back, so login
# would appear to succeed and then silently fail. prod.py sets both to True.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# --------------------------------------------------------------------------------------
# Object storage — MinIO
# --------------------------------------------------------------------------------------
# MinIO does not do virtual-host-style addressing (bucket.host) without extra DNS work,
# so force path-style (host/bucket). Real S3 accepts either; prod.py leaves the default.
AWS_S3_ADDRESSING_STYLE = "path"

# --------------------------------------------------------------------------------------
# DRF — browsable API
# --------------------------------------------------------------------------------------
# Adds the HTML renderer for hand-testing endpoints in a browser. JSON stays first so
# content negotiation still defaults to JSON, matching the deployed behaviour (API §1.2).
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
LOGGING["root"]["level"] = "DEBUG"
LOGGING["loggers"]["django"]["level"] = "DEBUG"
# django.db.backends at DEBUG logs every SQL statement — far too noisy for normal work.
# Set it to DEBUG deliberately when investigating a query, not by default.
LOGGING["loggers"]["django.db.backends"] = {
    "handlers": ["console"],
    "level": "INFO",
    "propagate": False,
}

# Email to stdout — nothing is sent from a dev box (verification codes, FR-1).
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
