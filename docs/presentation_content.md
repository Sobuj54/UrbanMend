# UrbanMend — Presentation Content

## Page 1 – Title Page

UrbanMend — An AI-Powered Crowd-Sourced System for Public Issue Prioritization

## Page 2 – Content List

- Introduction
- Motivation
- Objective
- Existing System Overview
- Drawbacks of Existing System
- Proposed System Design
- Proposed System Features
- Platform Overview
- Implementation
- Test Result
- Comparison
- Conclusion
- Future Work

## Page 3 – Introduction

- UrbanMend is a civic issue-reporting web platform for public infrastructure problems.
- Citizens submit descriptions, photos, and precise geolocation for issues such as potholes, lighting, waste, and drainage.
- A triage layer classifies reports and assigns Critical, High, Medium, or Low severity.
- Authorities receive issue queues and lifecycle tools; administrators manage accounts, moderation, and reference data.

## Page 4 – Motivation

- Crowd-sourced reports can be noisy, duplicated, bilingual, and difficult for authorities to triage.
- Authorities need a severity-ranked work queue instead of an unorganized collection of submissions.
- Duplicate clustering, corroboration counts, and nearby-landmark context support human decision-making.
- The project focuses on making public-issue triage explainable and auditable.

## Page 5 – Objective

- Enable fast, high-quality, geolocated, photo-backed citizen reporting.
- Automatically categorize reports and assign an explainable severity label.
- Cluster duplicate reports into one tracked Issue while preserving individual Reports.
- Support authority assignment, status management, moderation, notifications, and auditability.

## Page 6 – Existing System Overview

- Residents report issues via photo + GPS location
- Reports become tracked, categorized cases
- Used for non-emergency issues (potholes, lighting, etc.)
- Staff use maps to triage by neighborhood

## Page 7 – Drawbacks of Existing System

- No automated duplicate detection
- No AI-based severity classification
- Manual processing still required
- Limited backend customization
- City-specific apps don't work across cities

## Page 8 – Proposed System Design

- Modular monolith: Django/DRF API, bounded domain apps, PostgreSQL/PostGIS, Redis, and S3-compatible object storage.
- API handles authentication, validation, authorization, persistence, and reads; Celery workers handle slow or external work.
- Submission flow: validate and persist Report → enqueue after commit → classify with LLM or keyword fallback → cluster into Issue → process context and notifications.
- Core separation: Report stores a citizen submission; Issue stores clustered severity, status, assignment, and lifecycle state.

## Page 9 – Proposed System Features

- Session authentication, verification, password reset, role-based access control, authority provisioning, and optional two-factor authentication.
- Report and media APIs with validation, throttling, idempotency, image processing, thumbnails, and EXIF stripping.
- LLM classification adapter with deterministic bilingual keyword fallback; category and severity rationale are retained.
- Issue queue/map, clustering, confirmations, comments, assignment, status transitions, severity overrides, merge/split, moderation, notifications, audit, analytics, and exports.
  ![System design diagram](/docs/system-design.png)

## Page 10 – Platform Overview

- Python 3.13, Django 5.2 LTS, Django REST Framework 3.17, and ASGI served by Uvicorn.
- PostgreSQL 14+ with PostGIS provides the system of record and spatial queries/indexes.
- Celery 5.5 with Redis provides background jobs, cache, rate limits, and session caching; Celery Beat relays scheduled/outbox work.
- S3-compatible storage uses django-storages; Pillow handles images; drf-spectacular provides OpenAPI/Swagger; Docker Compose and Kubernetes/Azure deployment files are included.

## Page 11 – Implementation

- Django apps implement identity, reporting, media, classification, issues, geo, notifications, moderation, audit, export, and platform concerns.
- Views/serializers handle HTTP concerns; services enforce business rules, transactions, and authorization; selectors handle reads.
- Classification runs asynchronously and records category, severity, confidence, and classification state; fallback keywords prevent untriaged reports when the LLM is unavailable.
- Issue clustering uses spatial/category/time rules with concurrency protection; lifecycle changes emit immutable status and audit events.

## Page 12 – Test Result

- The repository contains 83 test files across API, identity, reporting, classification, issues, media, notifications, geo, audit, moderation, export, and platform modules.
- Tests cover authentication/RBAC, throttling, idempotency, classification, clustering concurrency, status transitions, privacy, notifications, geospatial APIs, and exports.
- Pytest, pytest-django, factory-boy, coverage tooling, Ruff, and mypy are configured in the project.
- No executed test-result report was found in the codebase, and pytest was not available in the current shell; numerical pass/fail results are therefore not stated.

## Page 13 – Comparison

| Aspect                 | Existing System                                   | Proposed UrbanMend System                                                |
| ---------------------- | ------------------------------------------------- | ------------------------------------------------------------------------ |
| Factual basis          | No specific existing system documented            | Implemented backend documented in code and architecture docs             |
| Triage                 | No verified implementation found in codebase/docs | Asynchronous LLM classification with deterministic keyword fallback      |
| Duplicate handling     | No verified implementation found in codebase/docs | Spatial/category/time clustering into Issues                             |
| Prioritization context | No verified implementation found in codebase/docs | Explainable severity plus display-only corroboration and POI proximity   |
| Governance             | No verified implementation found in codebase/docs | RBAC, moderation, immutable status/audit events, and authority overrides |

## Page 14 – Conclusion

- UrbanMend implements a structured backend for geolocated civic reporting and authority triage.
- The design combines asynchronous AI-assisted classification, deterministic fallback, spatial clustering, and explainable severity.
- Domain separation, service-layer authorization, audit records, moderation, and notification infrastructure support trust and maintainability.
- The repository provides API routes, persistence models, background tasks, deployment configuration, and broad automated-test coverage.

## Page 15 – Future Work

- Complete stakeholder sign-off and remaining production-readiness checklist items, including load, security, privacy, and failure-mode reviews.
- Build and run the held-out bilingual classifier evaluation; the documented accuracy/confidence bar remains an open decision.
- Complete later-phase roadmap work documented in `docs/05-project-plan.md`, including any explicitly deferred API or operational items.
- Continue hardening backups/restore, observability, notification delivery, LLM cost controls, and deployment rollback procedures.
