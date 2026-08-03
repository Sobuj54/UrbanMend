# PLACEHOLDER — written in A3 (T0.2). Intentionally not buildable: the base image tag
# depends on the Python version pinned in A2 (T0.1), which is an open decision.
#
# Mandated constraints (docs/06-devops-guide.md §2.2):
#   - Multi-stage build, python:3.x-slim base — NOT Alpine (GDAL/GEOS/PROJ and psycopg
#     have no musl wheels)
#   - Non-root user in the final stage
#   - ONE image for both processes; api vs worker is selected by the container `command`,
#     never by a second Dockerfile
#   - collectstatic runs at BUILD time, under settings that need no SECRET_KEY or
#     DATABASE_URL — no secrets exist at build time
#   - NEVER run `migrate` here or in an entrypoint. It is a separate pre-deploy step.
