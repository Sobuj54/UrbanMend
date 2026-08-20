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
    "drf_spectacular",
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
# Photo upload limits (T2.4/T2.5, FR-7, API §6.4)
# --------------------------------------------------------------------------------------
# ⚠️ **Our policy, not spec-derived.** FR-7 says "enforce size/type limits" and §6.4 names the
# statuses (`413`, `415`) without fixing a number, so these are defensible defaults kept in config
# (NFR-11) — the same resolution T1.2 took for the verification-code policy and T1.8 for the
# throttle rates. Raise them in `docs/04-api-specification.md` if they ever become a contract.

# 10 MiB. A modern phone camera JPEG is 2–6 MiB, so this accepts an unmodified photo from the
# devices PRD §5.2 targets while bounding what one request can push through the API pod.
# ⚠️ Django's own `DATA_UPLOAD_MAX_MEMORY_SIZE` does NOT bound a file upload — it exempts
# `request.FILES` — so without this check there is no size limit at all.
MEDIA_MAX_UPLOAD_BYTES = env.int("MEDIA_MAX_UPLOAD_BYTES", default=10 * 1024 * 1024)

# ⚠️ **An allowlist of formats we can actually re-encode, not a blocklist of dangerous ones.**
# The values are Pillow format names, checked against what Pillow *detects*, never against the
# client's `Content-Type` header — a `.php` renamed to `.jpg` arrives with an image content type and
# only the decoder can tell. SVG is absent deliberately: it is a script container, not a raster
# photo, and Pillow cannot strip anything from it.
MEDIA_ALLOWED_IMAGE_FORMATS = env.list(
    "MEDIA_ALLOWED_IMAGE_FORMATS", default=["JPEG", "PNG", "WEBP"]
)

# Longest edge of the stored image, in pixels. 2048 is enough for an Authority to read a house
# number off a photo on a desktop screen; beyond it the bytes buy nothing an operator can use
# (FR-7 "server-side compression").
MEDIA_MAX_DIMENSION = env.int("MEDIA_MAX_DIMENSION", default=2048)

# Longest edge of the thumbnail (FR-7). Sized for the Authority queue and map popups, where the
# whole point is that a list of 50 issues does not download 50 full photos (NFR-1).
MEDIA_THUMBNAIL_DIMENSION = env.int("MEDIA_THUMBNAIL_DIMENSION", default=320)

# JPEG/WebP quality for both derivatives. 82 is the usual "no visible artefacts" floor.
MEDIA_IMAGE_QUALITY = env.int("MEDIA_IMAGE_QUALITY", default=82)

# BR-3 needs at least one photo; nothing needs fifty. A cap keeps one report from turning the
# Authority queue into a gallery, and bounds the work T2.6's attach step does in one transaction.
MEDIA_MAX_PER_REPORT = env.int("MEDIA_MAX_PER_REPORT", default=5)

# --------------------------------------------------------------------------------------
# Collection search limits (T2.7 API §6.3, T7.1 API §6.5)
# --------------------------------------------------------------------------------------
# ⚠️ **Our policy, not spec-derived.** §6.3 and §6.5 both document `?nearLng=&nearLat=&radiusM=`
# without bounding the radius. This is a defensible default kept in config (NFR-11), like the media
# limits above.
#
# ⚠️ **Uncapped, `radiusM` is a free full-table spatial scan on a public-shaped read.** A radius
# larger than the planet makes `ST_DWithin` match every row, so the GiST index (T2.1) stops helping
# and the sort runs over the whole table on every request. 50 km bounds a search to "the whole
# city with room to spare" — Dhaka's metropolitan area is roughly 30 km across, and UrbanMend serves
# one city (PRD §11), so a larger value cannot express a question about *this* deployment.
#
# ⚠️ **The name reads narrower than the setting is: it bounds `?radiusM=` on *every* collection**,
# `/issues` included (T7.1). One number rather than a per-resource pair, deliberately — two would let
# the Issue and Report searches drift apart, and there is no reason a citizen may sweep a wider
# radius over one collection than the other. Renaming it is a three-deploy env-var change for no
# behavioural gain, so the comment carries the breadth instead.
REPORT_SEARCH_MAX_RADIUS_M = env.int("REPORT_SEARCH_MAX_RADIUS_M", default=50_000)

# --------------------------------------------------------------------------------------
# Issue proximity context (T7.1, FR-17, C-10)
# --------------------------------------------------------------------------------------
# ⚠️ **Our numbers, not spec-derived.** FR-17 asks for nearby-POI context and §6.5's example shows a
# single hospital 120 m away; neither fixes a radius or a count. Kept in config (NFR-11).
#
# ⚠️ **These are presentation limits and must never become policy.** C-10 makes proximity
# display-only: it may not affect severity, clustering or queue ordering, so widening the radius can
# only ever add context to a row, never move it. 500 m is walking distance — "the school is right
# there" — and beyond it "nearby" stops being a claim an operator can act on. Three entries is what a
# queue row can show without the POI list becoming the row.
ISSUE_PROXIMITY_RADIUS_M = env.int("ISSUE_PROXIMITY_RADIUS_M", default=500)
ISSUE_PROXIMITY_LIMIT = env.int("ISSUE_PROXIMITY_LIMIT", default=3)

# --------------------------------------------------------------------------------------
# Classification (T3.1–T3.3, FR-9/FR-10/FR-13a, NFR-13, Arch §6)
# --------------------------------------------------------------------------------------
# ⚠️ **Our numbers, not spec-derived.** NFR-13 requires a per-request token cap, a call rate limit, a
# response cache and a spend ceiling but names no values; Arch §6 requires "timeout + bounded retry
# with backoff" without fixing either. These are defensible defaults kept in config (NFR-11), like
# the media and search limits above.

# ⚠️ **❓Q9 is resolved as *deferred*: no provider is pinned, and this default is what makes that
# honest.** `UnconfiguredLLMProvider` raises `ClassificationUnavailable`, so a deployment with
# nothing set here classifies through FR-13a's keyword fallback rather than failing — which means
# the fallback is the *default* code path, not an emergency one that first runs during an incident
# (NFR-4, RISK-5). Setting this to a real provider is a deliberate act with a privacy decision
# attached (P7: no training on submitted data).
#
# A dotted path rather than a vendor name enum: adding a provider must not require editing this
# file, and `docs/07-adr-001` style says the seam is the deliverable, not the vendor.
CLASSIFICATION_LLM_PROVIDER = env(
    "CLASSIFICATION_LLM_PROVIDER",
    default="urbenmend.classification.llm.UnconfiguredLLMProvider",
)
CLASSIFICATION_LLM_ENDPOINT = env("CLASSIFICATION_LLM_ENDPOINT", default="https://api.openai.com/v1")
CLASSIFICATION_LLM_API_KEY = env("CLASSIFICATION_LLM_API_KEY", default="")
CLASSIFICATION_LLM_MODEL = env("CLASSIFICATION_LLM_MODEL", default="gpt-4o-mini")

# NFR-13's "cap tokens per request". The reply is four short fields (category, severity, confidence,
# one rationale sentence), so a large ceiling buys nothing but cost and a slower timeout when a model
# decides to explain itself at length.
CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS = env.int("CLASSIFICATION_LLM_MAX_OUTPUT_TOKENS", default=300)

# Per-attempt deadline. ⚠️ Well under `CELERY_TASK_SOFT_TIME_LIMIT` on purpose — with the retry
# below, the worst case is roughly two of these plus backoff, and O-2 requires triage never to block
# the queue. A provider that has not answered in ten seconds is indistinguishable from one that is
# down, and the keyword fallback is already sitting there.
CLASSIFICATION_LLM_TIMEOUT_SECONDS = env.float("CLASSIFICATION_LLM_TIMEOUT_SECONDS", default=10.0)

# Total attempts including the first, i.e. one retry. ⚠️ **Low deliberately: the alternative to
# retrying is not failure, it is the keyword fallback** (FR-13a), so a long retry chain spends money
# and queue time to avoid an outcome that is already acceptable. `1` disables retrying.
CLASSIFICATION_LLM_MAX_ATTEMPTS = env.int("CLASSIFICATION_LLM_MAX_ATTEMPTS", default=2)
CLASSIFICATION_LLM_BACKOFF_SECONDS = env.float("CLASSIFICATION_LLM_BACKOFF_SECONDS", default=0.5)

# Confidence the keyword fallback reports (FR-10 stores it; ❓Q10 — the accuracy bar and therefore
# the low-confidence review threshold — is **open**, so nothing here is tuned against a threshold
# that does not exist yet).
#
# ⚠️ 0.5 is the midpoint on purpose: any Q10 threshold above it sends every fallback classification
# to human review (T3.7), which is the conservative default while the LLM is the thing that is down.
CLASSIFICATION_FALLBACK_MATCHED_CONFIDENCE = env.float(
    "CLASSIFICATION_FALLBACK_MATCHED_CONFIDENCE", default=0.5
)
CLASSIFICATION_FALLBACK_UNMATCHED_CONFIDENCE = env.float(
    "CLASSIFICATION_FALLBACK_UNMATCHED_CONFIDENCE", default=0.1
)

# Redis-backed LLM controls (T3.4/T3.6). These are deployment policy rather than API contract.
# Cached responses do not consume a call or budget unit. Cache keys contain only SHA-256 digests;
# report text and user identifiers never appear in Redis keys.
CLASSIFICATION_LLM_CACHE_SECONDS = env.int("CLASSIFICATION_LLM_CACHE_SECONDS", default=86_400)
CLASSIFICATION_LLM_USER_RATE_LIMIT = env.int("CLASSIFICATION_LLM_USER_RATE_LIMIT", default=20)
CLASSIFICATION_LLM_GLOBAL_RATE_LIMIT = env.int("CLASSIFICATION_LLM_GLOBAL_RATE_LIMIT", default=500)
CLASSIFICATION_LLM_RATE_WINDOW_SECONDS = env.int(
    "CLASSIFICATION_LLM_RATE_WINDOW_SECONDS", default=3_600
)
# Provider-neutral spend guard. The worker reserves an estimated prompt+output token cost before
# calling the provider; the normal per-request output cap remains the hard bound for each call.
CLASSIFICATION_LLM_DAILY_TOKEN_BUDGET = env.int(
    "CLASSIFICATION_LLM_DAILY_TOKEN_BUDGET", default=1_000_000
)
CLASSIFICATION_LLM_CIRCUIT_FAILURE_THRESHOLD = env.int(
    "CLASSIFICATION_LLM_CIRCUIT_FAILURE_THRESHOLD", default=3
)
CLASSIFICATION_LLM_CIRCUIT_RECOVERY_SECONDS = env.int(
    "CLASSIFICATION_LLM_CIRCUIT_RECOVERY_SECONDS", default=60
)

# Q10 is still open, so no threshold is invented. Set a value in [0, 1] to activate T3.7's
# persisted review flag; an unset value records confidence without making a review decision.
CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD = env.float(
    "CLASSIFICATION_LOW_CONFIDENCE_THRESHOLD", default=None
)

# ⚠️ **The fallback's default severity band is deliberately NOT configurable.** It is a policy
# judgement argued out in `classification/keywords.py` (`DEFAULT_SEVERITY` = Medium: an unmatched
# report is unknown, not unimportant), and an env var here would invite an operator to set it to
# `critical` during an outage — putting every unrecognised report in the band FR-14/Q2 reserve for
# life-safety, which is how a Critical queue becomes noise nobody reads.

# --------------------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------------------
# API conventions (API §1.2, T0.6).
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
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

SPECTACULAR_SETTINGS = {
    "TITLE": "UrbanMend API",
    "DESCRIPTION": "Urban civic issue reporting, triage, moderation, and notification API.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
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
# Submission rate limiting (T2.9, FR-33, API §4.5)
# --------------------------------------------------------------------------------------
# ⚠️ **A separate dict from `AUTH_THROTTLE_RATES`, and not a rename of it.** The auth buckets are
# sized for credential guessing — five attempts a quarter hour. Borrowing them here would cut off a
# citizen photographing one flooded street after their fifth photo, so the two policies must be
# tunable independently by an operator reading settings. `ScopedWindowRateThrottle` merges both
# dicts by scope name, which is why the scopes are prefixed (`auth_*` / `submit_*`).
#
# ⚠️ **Also our policy, not spec-derived.** §4.5 requires "tighter buckets on … report submission
# (spam, FR-33)" and NFR-13 caps LLM cost, but neither fixes a number, and `api-conventions.md`
# lists "numeric rate limits and windows" under "do not invent". Same resolution as T1.2 and T1.8:
# defensible values, in config (NFR-11), labelled as chosen.
#
# ⚠️ **Every request counts, including one the endpoint then rejects.** DRF consumes the bucket in
# `allow_request()`, before the handler runs, so an out-of-city or malformed submission spends
# budget. That is correct for FR-33 — serving garbage costs the same as serving a real report — and
# it is why these windows are hours rather than minutes.
#
# ⚠️ **An idempotent replay also spends budget.** Exempting it would mean doing the §4.6 lookup
# inside a throttle's `get_cache_key()`, i.e. moving idempotency into the throttle layer. The
# `RateLimit-Remaining` header (§4.5) is the client's signal instead, and a retry burst is nowhere
# near these limits.
SUBMISSION_THROTTLE_RATES = {
    # Per-account, `POST /reports`. One report every three minutes, sustained for an hour, is far
    # above what a citizen walking a neighbourhood produces and far below what makes a spam script
    # worth writing. This is also the per-account LLM cost ceiling (NFR-13/RISK-3): triage runs once
    # per accepted report, so 20/h bounds what one account can spend.
    "submit_report": env("SUBMISSION_THROTTLE_RATE_REPORT", default="20/1h"),
    # Per-account, `POST /media`.
    #
    # ⚠️ **Deliberately NOT `MEDIA_MAX_PER_REPORT × submit_report`.** Making the report bucket the
    # binding one at sustained volume would leave the *expensive* endpoint effectively unlimited —
    # each upload costs a decode, a re-encode and a storage write, while a report costs one INSERT.
    # So at 60/h the upload bucket binds first for photo-heavy use (twelve five-photo reports an
    # hour) and the report bucket binds for text-only use. A single submission always fits inside
    # both (5 ≤ 60, 1 ≤ 20); only sustained volume meets either.
    "submit_media": env("SUBMISSION_THROTTLE_RATE_MEDIA", default="60/1h"),
    # Per-IP, and **shared by both endpoints** — 5 photos plus their report spend 6. That is what
    # makes it the Sybil bucket PRD §T3 asks for: a farm of fresh accounts has a fresh per-account
    # bucket each time, but the address does not change.
    #
    # ⚠️ **Sized for NAT, not for one household.** Mobile-carrier NAT in Bangladesh can put many
    # citizens behind one address (the tension `auth_anon` above already records), and a per-IP
    # submission limit that is too tight silences a whole neighbourhood during exactly the event —
    # a flood, a collapse — that produces a legitimate burst.
    #
    # ⚠️ **This bucket never sees anonymous traffic.** DRF's `initial()` runs `check_permissions()`
    # before `check_throttles()` (verified against the installed source), and both endpoints are
    # `IsAuthenticated`, so an anonymous flood is answered `401` before any counter moves. It costs
    # no rows, no bytes and no LLM calls, so there is nothing here to protect.
    "submit_ip": env("SUBMISSION_THROTTLE_RATE_IP", default="120/1h"),
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
CELERY_BEAT_SCHEDULE = {
    # A short polling interval keeps the notification SLA independent of API request volume.
    # Deployment runs exactly one beat scheduler; worker concurrency does not affect this lock-safe
    # relay because pending rows are claimed with SELECT ... FOR UPDATE SKIP LOCKED.
    "notifications-outbox-relay": {
        "task": "notifications.relay_outbox",
        "schedule": 10.0,
    },
}

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
