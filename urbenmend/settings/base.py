"""
Base settings — shared by dev / prod / build (A4, T0.3).

Everything environment-specific is read through django-environ so identical variable
names work locally and deployed [doc: DevOps §3.2]. There is no `settings.local`:
`base`/`dev`/`prod` is the resolved naming [doc: 08-coding-workflow.md A4].

`DJANGO_SECRET_KEY` and `DATABASE_URL` are REQUIRED with no fallback — a missing value
is a startup failure, not a silent insecure default. `build.py` puts throwaway values in
os.environ before importing this module, because no secrets exist at build time
[doc: DevOps §2.2].
"""

from pathlib import Path
from typing import Any

import environ
import structlog

# urbenmend/settings/base.py → settings/ → urbenmend/ → repo root.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# --------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

ROOT_URLCONF = "urbenmend.urls"
ASGI_APPLICATION = "urbenmend.asgi.application"
WSGI_APPLICATION = "urbenmend.wsgi.application"

# Models declare their own PK; this only silences the warning for anything that doesn't.
# ⚠️ API §1.2 requires opaque, non-sequential IDs in URLs — models exposed through the API
# take an explicit UUID PK. This default is NOT a licence to expose integer IDs.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ✅ SILENCED_SYSTEM_CHECKS is intentionally absent. A4 silenced `rest_framework.W001`
# (PAGE_SIZE set without DEFAULT_PAGINATION_CLASS); T0.6 set the pagination class in A8, so the
# warning no longer fires and the silencer was removed. `check --deploy` is clean with zero
# silenced checks — keep it that way so a real warning stays visible in CI (A9 / T0.5).

# --------------------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------------------
# Custom apps are listed before django.contrib so their templates take precedence over
# contrib's. `AUTH_USER_MODEL` (A6) is resolved by app label, not by position.
INSTALLED_APPS = [
    # One app per architecture module [doc: Arch §2.4]. Dashboard & Query intentionally has
    # no app — it is served by `issues` / `geo` selectors.
    # ⚠️ Nested under `urbenmend.` (not top-level) because a root-level `platform` package
    # would shadow the stdlib `platform` module that Django itself imports (A4 decision).
    "urbenmend.identity",
    "urbenmend.reporting",
    "urbenmend.media",
    "urbenmend.classification",
    "urbenmend.issues",
    "urbenmend.geo",
    "urbenmend.notifications",
    "urbenmend.moderation",
    "urbenmend.audit",
    "urbenmend.export",
    "urbenmend.platform",
    # Django contrib.
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",  # GeoDjango — NFR-1, PostGIS backend.
    # Third-party.
    "rest_framework",
    "rest_framework_gis",
    "django_otp",
    "django_otp.plugins.otp_totp",  # 2FA for Authority/Admin (FR-4).
    "django_prometheus",  # Observability — T0.9.
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    # Immediately after the metrics opener so every later middleware — including the error
    # paths — logs with a trace id already bound (T0.9) [doc: DevOps §8.3].
    "urbenmend.platform.middleware.TraceIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must follow AuthenticationMiddleware — it reads request.user to attach the verified
    # OTP device (FR-4). Present from the start so 2FA isn't retrofitted later.
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------
# PostGIS [doc: Arch §2.3, DevOps §3.1].
# ⚠️ The postgis:// scheme (not postgres://) selects django.contrib.gis.db.backends.postgis.
# Getting this wrong produces a confusing "unknown field type" error much later.
DATABASES: dict[str, dict[str, Any]] = {
    "default": env.db("DATABASE_URL"),
}
# Connection pooling — keep connections alive across requests (NFR-2).
DATABASES["default"]["CONN_MAX_AGE"] = 60
# Explicit engine check — fail fast if the DATABASE_URL scheme was wrong.
if DATABASES["default"]["ENGINE"] != "django.contrib.gis.db.backends.postgis":
    raise ValueError(
        f"DATABASE_URL must use the postgis:// scheme to select the GeoDjango engine. "
        f"Got ENGINE={DATABASES['default']['ENGINE']}. Check your .env.local or deployment config."
    )

# --------------------------------------------------------------------------------------
# Auth & sessions
# --------------------------------------------------------------------------------------
# ⚠️ Custom user model (A6 / T0.10 / T1.1). Declared BEFORE the first migration —
# irreversible afterwards; changing it later means dropping the database [doc: Arch §2.4].
# RBAC is the model's own `role` field plus an authority↔category scope relation evaluated
# in services.py — NOT contrib.auth Groups/Permissions, which cannot express BR-26 scoping.
AUTH_USER_MODEL = "identity.User"

# Password validation.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Password hashing — Argon2 (secure, A2/T0.1 dependency).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# --------------------------------------------------------------------------------------
# Caches
# --------------------------------------------------------------------------------------
# Redis (Arch §2.3). Used for: sessions (cached_db), reference data, rate limits.
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://redis:6379/0"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------
# Server-validated, cached_db backend (Arch §8, API §2).
# Opaque token in an httpOnly cookie; DB is the source of truth, cache speeds reads.
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_AGE = 86400  # 24 hours.
SESSION_COOKIE_HTTPONLY = True  # JS cannot read it (API §2).
SESSION_COOKIE_SAMESITE = "Lax"  # CSRF protection (API §2).
# SESSION_COOKIE_SECURE set per environment in dev.py / prod.py.

# --------------------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------------------
# Double-submit (API §2).
CSRF_USE_SESSIONS = False  # Separate CSRF cookie, not stored in the session.
CSRF_COOKIE_HTTPONLY = False  # JS must read it to send X-CSRFToken header.
CSRF_COOKIE_SAMESITE = "Lax"
# CSRF_COOKIE_SECURE set per environment in dev.py / prod.py.

# --------------------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------------------
# NFR-5, DevOps §8.1.
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# SECURE_SSL_REDIRECT, SECURE_HSTS_SECONDS set in prod.py only.

# --------------------------------------------------------------------------------------
# Internationalization
# --------------------------------------------------------------------------------------
# Bangla/English (NFR-8).
LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("bn", "বাংলা"),
]
TIME_ZONE = "UTC"  # Store all times in UTC; clients localize (NFR-8).
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------------------
# CSS, JavaScript, Images — collected at build time (Dockerfile).
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --------------------------------------------------------------------------------------
# Media files
# --------------------------------------------------------------------------------------
# User uploads — served from S3-compatible object storage (Arch §2.3).
# ⚠️ MEDIA_ROOT is mediafiles/, NOT media/ — media/ is a Django app name (A1 .gitignore fix).
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "mediafiles"

# Object storage — django-storages + S3-compatible (S3 or MinIO) [doc: Arch §2.3].
# STORAGES (not DEFAULT_FILE_STORAGE / STATICFILES_STORAGE) — those are removed in Django 6.0.
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    },
    # Static files stay on local disk: collectstatic runs at build time with no credentials
    # [doc: DevOps §2.2], so it cannot upload. Admin assets ship inside the image.
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# http://storage:9000 locally, the real S3 URL deployed.
AWS_S3_ENDPOINT_URL = env("STORAGE_ENDPOINT", default=None)
AWS_STORAGE_BUCKET_NAME = env("STORAGE_BUCKET", default="urbenmend-media")
# Ignored by MinIO, required by real S3.
AWS_S3_REGION_NAME = env("AWS_REGION", default="us-east-1")
# Credentials. Named STORAGE_* to match STORAGE_ENDPOINT/STORAGE_BUCKET rather than boto3's
# own AWS_* env vars, so one prefix covers the whole object-store config.
AWS_ACCESS_KEY_ID = env("STORAGE_ACCESS_KEY", default="")
AWS_SECRET_ACCESS_KEY = env("STORAGE_SECRET_KEY", default="")
AWS_S3_SIGNATURE_VERSION = "s3v4"
AWS_S3_FILE_OVERWRITE = False  # Never overwrite — each upload gets a unique key (P3).
AWS_DEFAULT_ACL = None  # Bucket-level policy controls access, not per-object ACLs.
AWS_QUERYSTRING_AUTH = True  # Presigned URLs for private objects (FR-7, NFR-12).
AWS_QUERYSTRING_EXPIRE = 3600  # 1 hour.

# --------------------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------------------
# API conventions (API §1.2, T0.6).
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",  # Cookie-based (API §2).
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",  # Explicit per-view override when public (Q7).
    ],
    # ✅ T0.6 (A8). Emits the `{data, page, meta}` envelope with opaque cursors that no DRF
    # built-in produces (API §1.3, §4.4). Cursor, not offset: the authority queue is sorted and
    # mutated concurrently, so offsets skip and repeat rows.
    "DEFAULT_PAGINATION_CLASS": "urbenmend.api.pagination.StandardCursorPagination",
    "PAGE_SIZE": 20,  # API §4.4 default; max 100 enforced by the pagination class.
    # ✅ T0.6 (A8). `{error: {code, message, details, traceId}}` for every error (API §4.2).
    "EXCEPTION_HANDLER": "urbenmend.api.exceptions.urbenmend_exception_handler",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",  # Photo upload (FR-7).
    ],
    # ISO-8601 UTC (API §1.2). DRF renders `Z` for UTC by default; stated for clarity.
    "DATETIME_FORMAT": "iso-8601",
    # ⚠️ The camelCase layer (API §1.2) is NOT a setting. DRF serializers emit snake_case, and
    # the docs call this gap "the single easiest way for the implementation to silently drift".
    # It is applied per-serializer via `urbenmend.api.serializers.CamelCaseSerializerMixin`,
    # because a global renderer-level rename would also rewrite keys that must stay verbatim —
    # `details[].field` values, which name the client's own submitted field, and GeoJSON's
    # fixed `type`/`geometry`/`coordinates`/`properties` keys (API §1.2, §4.3).
    #
    # ⚠️ `DEFAULT_THROTTLE_CLASSES`/`_RATES` are deliberately unset. T1.8 scopes rate limiting to
    # the auth endpoints (FR-4) and applies it per-view; a project-wide default would silently
    # throttle the public map and issue list, which API §4.5 does not ask for and Q7 makes
    # unauthenticated. Rates live in `AUTH_THROTTLE_RATES` below, read at throttle-instantiation
    # time so `override_settings` reaches them — see `urbenmend/api/throttling.py`.
}

# --------------------------------------------------------------------------------------
# Rate limiting (T1.8, FR-4, API §4.5)
# --------------------------------------------------------------------------------------
# ⚠️ **These numbers are our policy, not spec-derived.** `api-conventions.md` lists "numeric rate
# limits and windows" under "Not specified — do not invent", but FR-4 requires rate-limited login
# and the M1 gate requires it working. Same tension T1.2 resolved for the verification-code policy:
# choose defensible values, keep them in config (NFR-11), and label them so nobody later mistakes
# them for a contract. Raise them in `docs/04-api-specification.md` if they ever become one.
#
# ⚠️ Window syntax is `<count>/<n><unit>` and is parsed by `ScopedWindowRateThrottle`, NOT by DRF:
# DRF's `parse_rate` reads only the first character of the period, so "5/15m" there would mean
# 5-per-minute with the 15 silently dropped.
#
# Backed by the Redis `default` cache above. A cache flush resets the counters — acceptable, since
# these are backoff windows rather than durable lockout state (the T1.8 lockout decision).
AUTH_THROTTLE_RATES = {
    # Per-IP, across all identifiers. Sized to absorb a household or small office behind one NAT
    # while still capping a single source's spray. Mobile-carrier NAT in Bangladesh can put many
    # users behind one address, which is why the per-identifier bucket below is the tighter one.
    "auth_anon": env("AUTH_THROTTLE_RATE_ANON", default="10/15m"),
    # Per-identifier — the FR-4 brute-force limit. Cleared on a successful login, so a legitimate
    # user who mistypes a few times is not held out once they get it right.
    "auth_identity": env("AUTH_THROTTLE_RATE_IDENTITY", default="5/15m"),
    # Per-session, for auth actions taken with a session already in hand.
    "auth_user": env("AUTH_THROTTLE_RATE_USER", default="20/15m"),
}

# --------------------------------------------------------------------------------------
# Idempotency (T2.3, BR-5, API §4.6)
# --------------------------------------------------------------------------------------
# ⚠️ **Also our policy, not spec-derived** — `api-conventions.md` names the "idempotency-key
# retention window" under "Not specified — do not invent", and §4.6 was amended to say explicitly
# that the window is deployment configuration rather than contract. Clients must treat a key as
# valid for their own retry burst only.
#
# Backed by the Redis `default` cache above (`urbenmend/api/idempotency.py`). A cache flush drops
# every held key: in-flight requests then behave as first uses, so a client retrying across the
# flush can create a second Report. Acceptable for the same reason as the throttle counters — this
# is a retry window, not durable state — but it is why a flush is not a routine operation.
#
# 24 hours. Long enough to cover a phone that lost connectivity mid-submission and retries when it
# comes back, short enough that Redis is not accumulating a record per submission indefinitely.
IDEMPOTENCY_RETENTION_SECONDS = env.int("IDEMPOTENCY_RETENTION_SECONDS", default=86_400)
# How long an unfinished request holds its key before another attempt may claim it. This is the
# backstop for a process killed between `reserve()` and `complete()`/`release()` — the ordinary
# paths return the key themselves. ⚠️ Must stay comfortably above the worst-case duration of a
# `POST /reports` transaction: too low and a slow request's own retry starts a second write while
# the first is still running, which is the duplicate BR-5 exists to prevent.
IDEMPOTENCY_IN_PROGRESS_SECONDS = env.int("IDEMPOTENCY_IN_PROGRESS_SECONDS", default=60)
# ⚠️ A bound, not a format. §4.6 does not constrain the key's *shape* — a UUID, a ULID and a
# client-composed string are all legitimate — so this only stops an unbounded header becoming an
# unbounded cache write. Over-long keys are rejected `400`, never truncated (truncation would alias
# two distinct keys onto one record).
IDEMPOTENCY_KEY_MAX_LENGTH = env.int("IDEMPOTENCY_KEY_MAX_LENGTH", default=255)

# --------------------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------------------
# Async worker + beat (Arch §2.3, T0.7).
# ⚠️ Redis db 1, separate from the cache on db 0 — `redis-cli FLUSHDB` on the cache must
# not discard queued jobs.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/1")
# No result backend: tasks report through domain state and the outbox (Arch §7.1), never
# through a Celery result. Enabling one would add writes nothing reads.
CELERY_RESULT_BACKEND = None
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300  # 5 minutes hard limit.
CELERY_TASK_SOFT_TIME_LIMIT = 270  # 4.5 minutes soft limit.

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
# Structured JSON to stdout (T0.9, DevOps §8.2).
# structlog wired through Django's LOGGING so framework and third-party loggers land in the same stream.
LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "structlog.stdlib.ProcessorFormatter",
            # A callable, not a dotted string — ProcessorFormatter calls `processor`
            # directly and never resolves an import path.
            "processor": structlog.processors.JSONRenderer(),
            "foreign_pre_chain": [
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.format_exc_info,
            ],
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# structlog configuration (T0.9).
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
