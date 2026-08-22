# UrbanMend — Presentation Content (Simplified)

## Page 1 – Title Page

UrbanMend — AI-Powered Crowd-Sourced System for Public Issue Prioritization

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

- Web platform for reporting public issues
- Citizens submit photo, description, GPS location
- Covers potholes, lighting, waste, drainage
- Auto-assigns severity: Critical, High, Medium, Low
- Authorities get queues; admins manage system

## Page 4 – Motivation

- Crowd reports are noisy and duplicated
- Manual triage is slow and inconsistent
- Need severity-ranked work queue
- Duplicate detection helps prioritize faster
- Focus on explainable, auditable triage

## Page 5 – Objective

- Fast, photo + GPS based reporting
- Auto-categorize issues with severity label
- Cluster duplicate reports into one Issue
- Support assignment, status, and notifications
- Keep full audit trail

## Page 6 – Existing System Overview

- Residents report via photo + GPS
- Reports become tracked, categorized cases
- Used for potholes, lighting, etc.
- Staff triage using neighbourhood maps

## Page 7 – Drawbacks of Existing System

- No automated duplicate detection
- No AI-based severity classification
- Manual processing still required
- Limited backend customization
- Apps don't work across cities

## Page 8 – Proposed System Design

![System design diagram](/docs/system-design.png)
![ERD](/docs/erd.png)

## Page 9 – Proposed System Features

- Secure login + role-based access
- Report & media upload with validation
- AI classification with fallback rules
- Issue map, clustering, assignment, alerts

## Page 10 – Platform Overview

- Python 3.13, Django 5.2, DRF
- PostgreSQL + PostGIS (spatial data)
- Redis + Celery (background jobs)
- Docker & Kubernetes for deployment

## Page 11 – Implementation

- Modular Django apps per domain
- Services handle business logic & rules
- Async classification with fallback safety
- Clustering + audit-logged status changes

## Page 12 – Test Result

- 83 test files across all modules
- Covers auth, classification, clustering, APIs
- Uses Pytest, coverage tools, linting
- Full pass/fail results not yet generated

## Page 13 – Comparison

| Feature             | Existing System             | UrbanMend                  |
| ------------------- | --------------------------- | -------------------------- |
| Reporting           | Photo + GPS                 | Photo + GPS                |
| Duplicate Detection | Manual                      | Automatic (AI clustering)  |
| Severity Rating     | Manual                      | AI + fallback rules        |
| Triage              | By neighbourhood map        | By severity + location     |
| Customization       | Limited                     | Fully customizable backend |
| Interoperability    | City-specific, not portable | Modular, adaptable design  |
| Audit Trail         | Limited                     | Full status/audit logging  |

## Page 14 – Conclusion

- Structured backend for civic reporting
- Combines AI classification + clustering
- Built for trust: audit, moderation, RBAC
- Backed by strong automated test coverage

## Page 15 – Future Work

- Complete production-readiness checklist
- Run bilingual classifier evaluation
- Finish later-phase roadmap items
- Harden backups, alerts, and rollback
