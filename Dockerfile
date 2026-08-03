# UrbanMend — one image, two processes (A3 / T0.2).
#
# The api and worker containers run this SAME image and differ only by `command`
# [doc: DevOps §2.1]. That guarantees the parity the pipeline depends on: a Report
# written by the API and read by the worker must see identical models and migrations.
#
# Base is python:3.13-slim (A2/T0.1) = Debian 13 "trixie". NOT Alpine — GDAL/GEOS/PROJ
# and psycopg have no musl wheels and would compile from source [doc: DevOps §2.2].
#
# ⚠️ `migrate` is NOT run here and NOT in an entrypoint. It is a separate pre-deploy
# step [doc: DevOps §2.2/§7] — N replicas starting at once would race each other.

# ---------------------------------------------------------------------------
# Stage 1 — build wheels. Compiles C extensions once, keeps toolchain out of runtime.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# -dev packages are build-time only; the runtime stage installs shared libs instead.
RUN apt-get update && apt-get install --no-install-recommends -y \
      build-essential \
      libpq-dev \
      libgdal-dev \
      libgeos-dev \
      libproj-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Lock files only — this layer rebuilds when dependencies change, not when app code does.
COPY requirements/base.txt requirements/base.txt
RUN pip wheel --wheel-dir /wheels --require-hashes -r requirements/base.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# GeoDjango dlopen()s these at runtime — shared libraries, not the -dev headers.
# ⚠️ Package names are Debian 13 (trixie) specific and were verified in-image:
# libgdal36 / libgeos-c1t64 supersede the libgdal32 / libgeos-c1v5 of Debian 12.
# Re-verify with `apt-cache search` if the base image's Debian release ever changes.
RUN apt-get update && apt-get install --no-install-recommends -y \
      libpq5 \
      gdal-bin \
      libgdal36 \
      libgeos-c1t64 \
      libproj25 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app app

WORKDIR /app

COPY --from=deps /wheels /wheels
COPY requirements/base.txt requirements/base.txt
# --no-index: install strictly from the wheels built in stage 1, never from the network.
RUN pip install --no-index --find-links=/wheels -r requirements/base.txt \
    && rm -rf /wheels

COPY . .

# Build-time only. Uses urbenmend.settings.build, which must NOT require SECRET_KEY or
# DATABASE_URL — no secrets exist at build time [doc: DevOps §2.2]. Written in A4/T0.3.
RUN DJANGO_SETTINGS_MODULE=urbenmend.settings.build \
    python manage.py collectstatic --noinput

# Non-root from here on [doc: DevOps §2.2, NFR-5].
USER app

EXPOSE 8080

# Default process is the API; the worker overrides `command`:
#   api    → uvicorn urbenmend.asgi:application --host 0.0.0.0 --port 8080
#   worker → celery -A urbenmend worker --loglevel=info
CMD ["uvicorn", "urbenmend.asgi:application", "--host", "0.0.0.0", "--port", "8080"]

# ---------------------------------------------------------------------------
# Stage 3 — dev/CI. Adds the test + lint toolchain on top of runtime.
#
# Kept as a stage AFTER runtime so `--target runtime` (the deployed image) can never
# pick up pytest/ruff/mypy/pip-tools. docker-compose.override.yml and CI target this;
# production targets `runtime`.
# ---------------------------------------------------------------------------
FROM runtime AS dev

# root only for the install; drops back to app below.
USER root

# dev.txt is compiled with --allow-unsafe, so it pins pip/setuptools; --no-deps is not
# used because the dev toolchain's own transitive deps are already locked in the file.
COPY requirements/dev.txt requirements/dev.txt
RUN pip install --require-hashes -r requirements/dev.txt

USER app
