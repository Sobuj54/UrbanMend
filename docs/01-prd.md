# UrbanMend — Product Requirements Document (PRD)

> An AI-Powered Crowd-Sourced System for Public Issue Prioritization

| | |
|---|---|
| **Document** | `docs/01-prd.md` |
| **Version** | 1.1 (Draft for review) |
| **Status** | Planning phase — pending stakeholder sign-off |
| **Author role** | Senior Backend Architect |
| **Date** | 2026-07-22 |
| **Source of truth** | `PROJECT PROPOSAL.pdf` (submitted 2026-01-11) |
| **Supersedes** | v1.0 |
| **Next document** | `docs/02-*` (NOT to be written yet) |

### Changelog
- **v1.1 (2026-07-22)** — Two decisions incorporated: (1) AI is a **hosted LLM API** (OpenAI/Claude/Gemini), **not** a custom-trained model; (2) the **weighted numeric priority score is removed** — issues are triaged by an LLM-assigned **severity label**; frequency and proximity are retained as **display-only context**, not score inputs. See §16 and §18.
- **v1.0 (2026-07-22)** — Initial draft.

---

## 0. How to Read This Document

This PRD defines **what** UrbanMend must do and **why**, not **how** it will be built. Architecture, schema, and API contracts belong in later documents. Because I am authoring this as a Senior Backend Architect, I have surfaced a small number of **architecturally load-bearing constraints** (e.g. geospatial handling, severity auditability) inside the requirements, because ignoring them at PRD stage would produce a product that cannot later be built correctly.

Sections needing your attention before this document is frozen are marked **⚠️ DECISION NEEDED** or **❓ OPEN QUESTION**.

---

## 1. Executive Summary

UrbanMend is a **civic issue-reporting web platform** for a single city (initial deployment: Bangladesh) in which citizens report public infrastructure problems (potholes, broken streetlights, waste, water/drainage, etc.) with a photo and a precise geolocation. The system's differentiator is an **AI-assisted triage layer** that automatically (a) **categorizes** each report and (b) assigns a **severity level** (High / Medium / Low), so municipal authorities can work the most urgent issues first. The AI is a **hosted large-language-model (LLM) API**, not a custom-trained model.

The platform serves three actors: **Citizens** (report and track), **Authorities** (triage, act, and resolve — simulated/admin-provisioned roles for this prototype), and **Platform Administrators** (provision authority accounts, moderate, manage reference data).

The core product bet: *reporting is a solved problem; **triage** is not.* UrbanMend's value is concentrated in turning noisy, bilingual, crowd-sourced text into a clean, categorized, severity-ranked, de-duplicated work queue — with the corroboration count and nearby-landmark context shown so authorities can exercise judgment.

---

## 2. Goals and Non-Goals

### 2.1 Product Goals

| # | Goal | Success looks like |
|---|------|--------------------|
| G1 | Let any citizen file a high-quality, geolocated, photo-backed report in under 90 seconds. | Median submission time < 90s; < 5% of reports rejected for missing location/photo. |
| G2 | Automatically categorize reports with useful accuracy. | ≥ 85% top-1 category agreement with human labels on a held-out set (target; see risks). |
| G3 | Assign every issue a **clear, defensible severity level**. | Every issue shows a severity (High/Med/Low) with the recognized signals behind it; authorities can override with a logged reason. |
| G4 | Cluster duplicate/related reports of the same real-world issue. | Duplicate reports of one incident collapse into a single tracked issue ≥ 80% of the time. |
| G5 | Give authorities a severity-ranked, map-based work queue with corroboration & proximity context. | Authorities can find, filter, assign, and resolve the top issues in ≤ 3 clicks; "N reporters" and nearby landmarks are visible per issue. |
| G6 | Keep citizens informed of status changes. | Citizens receive a notification within 1 minute of any status change on their report. |
| G7 | Make the whole loop **auditable and trustworthy**. | Every state change and severity override is logged with actor, timestamp, and reason. |

### 2.2 Non-Goals (Explicitly Out of Scope)

Carried from the proposal and extended:

- **Native mobile apps** (iOS/Android). The product is a **mobile-responsive web app** (PWA-capable is a stretch goal).
- **Physical repair / field dispatch logistics** (crew scheduling, work orders, fleet tracking).
- **Government financial / budgeting system integration.**
- **Emergency / life-safety response.** UrbanMend is explicitly **NOT** a 999/emergency service. Stated in-product (RISK-1).
- **Real inter-agency routing to live municipal back-offices.** Authorities are **simulated, admin-provisioned roles**.
- **Multi-city / multi-tenant operation.** Single-city only (the data model should not actively prevent a future city column — §11).
- **Training or hosting a custom ML/NLP model.** *(New in v1.1.)* Classification and severity come from a hosted LLM API. No dataset collection, training pipeline, or model hosting is in scope.
- **A weighted numeric priority-scoring engine.** *(New in v1.1.)* There is no 0–100 score, no tunable weights, and no score-recomputation subsystem. Triage is by severity label; frequency/proximity are display-only.
- **Payments, fines, or citizen rewards/gamification** (deferred).

---

## 3. Assumptions

> Explicit **assumptions**, not confirmed facts. If any is wrong, the affected requirements must be revisited.

| # | Assumption | Impact if false |
|---|-----------|-----------------|
| A1 | Target users are in a **single city in Bangladesh**; UI and report text include **Bangla and English** (code-mixed "Banglish" is common). | Changes classification approach, notification channels, fonts. |
| A2 | **Smartphone + mobile data** dominates access; connectivity may be intermittent. | Justifies offline-tolerant submission, image compression, low-bandwidth mode. |
| A3 | Authorities are **not real municipal partners** during the prototype; roles are provisioned by an admin. | If real, adds legal, procurement, SLA, and integration requirements. |
| A4 | The AI layer is a **hosted LLM API** (OpenAI / Claude / Gemini) used for **categorization + severity labelling**, with **keyword rules as a deterministic fallback** when the API is unavailable or over budget. **No custom model is trained or hosted.** | Determines integration, cost, privacy, and fallback requirements. Replaces v1.0's "trained ML classifier." |
| A5 | We may **seed static reference data** (hospitals, schools, highways) from OpenStreetMap or a provided dataset; approximate data is acceptable. Used **only to display proximity context**, not to compute a score. | Proximity display quality and licensing depend on this. |
| A6 | **SMS and/or email** are acceptable notification channels; push is optional. | Affects notification cost, deliverability, phone-number verification. |
| A7 | Expected prototype scale is **modest** (hundreds–low thousands of reports, tens of concurrent authority users). | Sizing/caching/scaling change materially at high scale; also keeps LLM API cost low. |
| A8 | Photos may contain **people, faces, plates, homes** — personal data — and reports are at least partially **publicly visible**. | Drives privacy, moderation, data-protection requirements (§9). |
| A9 | Citizen identity can be **lightweight** (email or phone verification); no national ID/KYC to report. | Anonymous/low-friction vs abuse trade-off (§7, RISK-3). |
| A10 | This is an **academic capstone**: deliverable is a working prototype + technical report + user manual, judged on demonstrable functionality and the AI methodology — not production hardening. | Reprioritizes MUST vs SHOULD; sets realistic accuracy targets. |
| A11 | Report text (possibly containing personal details) **leaves our server** to a third-party LLM provider for classification. | Introduces a data-protection/privacy consideration (§9, RISK-12) and an external-dependency risk (RISK-5). |

---

## 4. Users, Personas, and Roles

### 4.1 Personas

- **Rahim — the Citizen Reporter.** Commuter, mid-range Android, intermittent data. Wants to report a dangerous pothole in 60 seconds and *know someone saw it*.
- **Fatima — the Authority Officer.** Overwhelmed by volume. Needs a **severity-ranked**, filterable queue and a map of issues, with "how many people reported this" and "what's nearby" visible so she can trust and adjust the ordering.
- **Karim — the Platform Administrator.** Provisions/verifies authority accounts, moderates abusive/duplicate content, and manages reference data (POIs, severity keyword lists).

### 4.2 Role & Permission Matrix

| Capability | Anonymous | Citizen | Authority | Admin |
|---|:--:|:--:|:--:|:--:|
| View public reports / map | ✅ | ✅ | ✅ | ✅ |
| Submit a report | ⚠️ (❓Q4) | ✅ | ✅ | ✅ |
| Track own reports / get notified | — | ✅ | ✅ | ✅ |
| Confirm / "me too" on an issue | — | ✅ | ✅ | ✅ |
| Comment / add info | — | ✅ | ✅ | ✅ |
| View full authority dashboard & map | — | — | ✅ | ✅ |
| Change report status / assign | — | — | ✅ | ✅ |
| **Override severity (with reason)** | — | — | ✅ | ✅ |
| Provision/verify authority accounts | — | — | — | ✅ |
| Moderate / merge / split / delete reports | — | — | (limited) | ✅ |
| Manage reference data (POIs, keyword lists) | — | — | — | ✅ |
| View audit log | — | — | (own actions) | ✅ |

---

## 5. Functional Requirements

IDs (`FR-x`) are stable. Priorities use MoSCoW (**MUST / SHOULD / COULD**). Requirements removed in v1.1 are retained as tombstones for traceability.

### 5.1 Authentication & Accounts

- **FR-1 (MUST) — Citizen registration & login.** Email or phone sign-up with verification (OTP/link), secure password storage, sessions, password reset.
- **FR-2 (MUST) — Authority account provisioning.** Authority accounts are **created/verified by an Admin** (not self-serve) and may be scoped to one or more **categories/departments**.
- **FR-3 (MUST) — Role-based access control (RBAC).** Enforce §4.2 on **every** server-side action, not just in the UI.
- **FR-4 (SHOULD) — Account security baseline.** Rate-limited login, lockout/backoff, optional 2FA for authorities/admins.

### 5.2 Report Submission ("Smart Report")

- **FR-5 (MUST) — Create a report.** Fields: description, category (optional — AI suggests), photo(s), location, server-authoritative timestamp. Cannot submit without a valid location and at least one of {photo, adequate description}.
- **FR-6 (MUST) — Location capture.** Auto-detect via device GPS **and** manual pin-drop/adjust on a map; store precise coordinates + reverse-geocoded address.
- **FR-7 (MUST) — Photo upload & handling.** Accept common formats; enforce size/type limits; server-side compression + thumbnails; strip/retain EXIF per privacy rule (P3).
- **FR-8 (SHOULD) — Resilient submission.** Tolerate flaky connectivity; queue offline submissions and sync (PWA stretch).
- **FR-9 (COULD) — Voice / "quick report"** for accessibility and low-literacy users.

### 5.3 AI Classification (Hosted LLM API)

- **FR-10 (MUST) — Automatic categorization & severity.** On submission, call the **LLM API** to return `{ category, severity, confidence }` for the report. Store the values, the confidence, and the **model/provider + version** used. Runs **asynchronously** (NFR-3) so submission stays fast.
  - *Accept:* every report gets a category and severity; low-confidence results are flagged for human confirmation.
- **FR-11 (MUST) — Human-in-the-loop correction.** Citizens may override category/severity at submission; authorities/admins may re-categorize or re-severity. Corrections are **logged** and can seed prompt examples / evaluation sets.
- **FR-12 (MUST) — Bilingual understanding.** Handle Bangla, English, and code-mixed "Banglish" (A1) — a key reason for using a hosted LLM rather than a self-trained model.
- **FR-13 (COULD) — Image-assisted classification.** Use a **vision-capable LLM** to corroborate category/severity from the photo. Explicitly optional / research stretch.
- **FR-13a (MUST) — Deterministic fallback.** If the LLM API is unavailable, times out, or the request is over budget, a **keyword-rule fallback** still assigns a category and severity so no issue is left untriaged (pairs with NFR-4).

### 5.4 Severity, Corroboration & Context *(replaces v1.0 "Priority Scoring Engine")*

> **Design principle:** There is **no computed numeric score and no tunable weights.** Triage is driven by the **severity label**. Corroboration and proximity are shown to authorities as **context for human judgment**, not combined into a number. This keeps triage transparent and trivially explainable.

- **FR-14 (MUST) — Severity label.** Every issue carries a severity of **High / Medium / Low** (❓Q2 — confirm whether a "Critical" band is needed). Severity comes from FR-10 (LLM) or FR-13a (fallback), reflecting high-risk indicators ("danger", "accident", "flood", "gas leak", "live wire", "collapse") in **both Bangla and English**.
  - *Accept:* every issue shows a severity plus the recognized indicators behind it ("flagged High: 'live wire', 'children'").
- **FR-15 (MUST) — Severity is explainable.** The UI shows *why* a severity was assigned (the key phrases/category that drove it). No black-box ranking.
- **FR-16 (SHOULD) — Corroboration count (display-only).** Show how many **distinct reporters** have reported the same clustered issue (e.g. "6 people reported this"). This is **informational**; it does **not** change severity. Count distinct trustworthy reporters, not raw submissions (T1).
- **FR-17 (SHOULD) — Proximity context (display-only).** Show nearby **sensitive landmarks** (hospital/school/highway/market) on the issue (e.g. "≈120 m from Dhaka Medical"). **Informational only**; does not change severity. Uses seeded POIs (A5).
- **FR-18 (MUST) — Duplicate/related clustering.** Group reports describing the same physical issue by **spatial proximity + category + time window** (optionally text similarity), tracked as **one Issue** with many contributing reports. Required to produce the FR-16 count and keep the queue clean; each citizen still tracks "their" report.
- **FR-19 (SHOULD) — Aging visibility.** Show how long an issue has been open / in each status, and allow sorting by age, so severe-but-old issues aren't forgotten. (No score; sorting/visibility only.)
- **FR-20 (MUST) — Authority severity override.** Authorities/admins can raise/lower an issue's severity with a **mandatory reason**; the LLM-assigned original and the override (actor, reason, timestamp) are both retained and shown.
- **FR-21 — REMOVED (v1.1).** *(Was: configurable scoring weights.)* No weighted score exists, so there are no weights to configure. Severity keyword lists (FR-14) may be admin-managed instead — see FR-30.

### 5.5 Authority Dashboard

- **FR-22 (MUST) — Severity-ranked work queue.** A filterable, sortable list of **Issues**, default-sorted by **severity** (then age), scoped to the authority's department(s). Each row surfaces the FR-16 corroboration count and FR-17 proximity context.
- **FR-23 (MUST) — Issue map / hotspots.** Geospatial map of issues, filterable by category/severity/status/area (the proposal's "heat map"/"hotspots").
- **FR-24 (MUST) — Issue lifecycle management.** Move issues through the §6.3 workflow; assign to self/department; internal notes + public updates.
- **FR-25 (SHOULD) — Merge & split.** Merge mis-clustered duplicates; split incorrectly merged clusters; bulk status updates.
- **FR-26 (SHOULD) — Basic analytics.** Counts by category/severity/status/area; median time-to-resolution; open-vs-resolved trends.

### 5.6 Notifications

- **FR-27 (MUST) — Status-change notifications.** Notify reporting citizen(s) on every status transition (email/SMS per A6 + in-app), within 1 minute.
- **FR-28 (SHOULD) — Notification preferences.** Choose channels; opt out (transactional vs marketing).
- **FR-29 (COULD) — Authority alerts.** Alert the relevant department when a **High-severity** issue is created or a cluster's reporter count crosses a threshold.

### 5.7 Administration, Moderation & Trust

- **FR-30 (SHOULD) — Reference-data management.** Admin manages **POIs** (FR-17) and **severity keyword lists** (FR-14). *(Replaces the removed scoring-weights UI.)*
- **FR-31 (MUST) — Content moderation.** Admin can hide/remove abusive, illegal, or privacy-violating content, with reason logged.
- **FR-32 (MUST) — Audit log.** Immutable, append-only log of auth events, role grants, status changes, **severity overrides**, moderation, and reference-data changes — actor, timestamp, before/after.
- **FR-33 (SHOULD) — Abuse controls.** Rate-limit submissions; detect spam/duplicate floods; reporter trust signals feeding FR-16.
- **FR-34 (COULD) — Public transparency page.** Public read-only stats/map fulfilling the transparency objective.

---

## 6. Domain Model, Taxonomy & Lifecycle (Product-Level)

### 6.1 Core Entities (conceptual)

- **User** (Citizen / Authority / Admin)
- **Report** — a single citizen submission (text, photos, location, category, severity inputs).
- **Issue** — a **cluster** of one or more Reports = one real-world problem. ***Severity*, status, and assignment live on the Issue**, not the individual Report. (This distinction remains critical; the proposal's model must not treat "report" and "issue" as synonyms.)
- **Category** — taxonomy node (below).
- **Point of Interest (POI)** — seeded landmarks, used **only** to display proximity context (FR-17).
- **StatusEvent / AuditEvent** — lifecycle and integrity records.
- **Notification** — a delivered/queued message.

### 6.2 Proposed Category Taxonomy (draft — ❓ needs confirmation)

`Roads & Transport` · `Street Lighting` · `Water & Drainage` · `Sanitation & Waste` · `Electrical Hazards` · `Public Structures` · `Other / Uncategorized`.

### 6.3 Issue Lifecycle (state machine)

```
SUBMITTED → TRIAGED → ACKNOWLEDGED → IN_PROGRESS → RESOLVED → CLOSED
                                   ↘ REJECTED / DUPLICATE / INSUFFICIENT_INFO
```
- Only Authorities/Admins advance past `TRIAGED`. `DUPLICATE` links to the surviving issue. Reopen from `RESOLVED`/`CLOSED` with reason (**SHOULD**). Every transition emits a StatusEvent → notifications (FR-27) + audit (FR-32).

---

## 7. Trust, Anti-Abuse & Fairness (Cross-Cutting)

Removing the numeric score **lowers** — but does not eliminate — gaming stakes, because frequency is now display-only rather than a ranking input.

- **T1 — Honest corroboration count.** FR-16 counts **distinct trustworthy reporters**; ignore rapid duplicates from one account/device/IP so the "N reporters" figure can't be trivially inflated.
- **T2 — Reporter trust signal.** Newer/unverified accounts weigh less toward the corroboration count.
- **T3 — Sybil resistance.** Verification (FR-1) + rate limits (FR-33) + device/IP heuristics.
- **T4 — Equity/representation bias.** Crowd-sourcing over-represents affluent, connected areas (cited Pak & Chua 2017). Because triage is now **severity-first** rather than volume-driven, this bias is naturally reduced — a genuine upside of the severity-only decision. Admins SHOULD still monitor coverage gaps.
- **T5 — Explainability as trust.** Severity is inherently simple to explain (FR-15); unexplained rankings can't be audited or contested.
- **T6 — No silent suppression.** Triage may **reorder** work; it must never **hide** a reported hazard. Low severity ≠ invisible.

---

## 8. Non-Functional Requirements (NFRs)

| # | Category | Requirement |
|---|----------|-------------|
| NFR-1 | **Geospatial capability** | MUST support efficient spatial queries (radius/nearest-POI for FR-17, clustering for FR-18, map density for FR-23) as a **first-class** capability. *Architect's note: still strongly implies a spatially-capable datastore (e.g. PostgreSQL + PostGIS), even though there is no score.* |
| NFR-2 | Performance | Interactive pages < 2s on a mid-range mobile over 3G/4G at prototype scale (A7); map/queue paginated. |
| NFR-3 | Classification latency | LLM categorization/severity runs **asynchronously**; the issue appears immediately and updates when the LLM (or fallback) returns, within a few seconds. |
| NFR-4 | Availability / degradation | If the LLM API is down/over budget, the **keyword fallback** (FR-13a) still assigns category + severity. The product never hard-depends on the external API. |
| NFR-5 | Security | HTTPS everywhere; hashed passwords; server-side authorization on every action; input validation; OWASP Top 10; secrets (incl. LLM API keys) never in code. |
| NFR-6 | Privacy & data protection | PII minimization; documented retention; user/PII deletion; photo-EXIF/face handling (§9); disclosure that text is sent to an external LLM (A11). |
| NFR-7 | Accessibility | WCAG 2.1 AA target; one-handed mobile use; correct Bangla typography; color-blind-safe map/severity legend. |
| NFR-8 | Localization | Full Bangla + English UI; locale-aware dates/numbers. |
| NFR-9 | Observability | Structured logging, error tracking, metrics for report throughput, classification outcomes, LLM latency/cost, notification delivery. |
| NFR-10 | Auditability | Integrity-relevant actions immutably logged (FR-32); severity overrides fully traceable. |
| NFR-11 | Maintainability / config | Category taxonomy, POI data, and severity keyword lists are data/config, not hard-coded (FR-30). *(No scoring weights — removed.)* |
| NFR-12 | Data portability | Reports/issues exportable (CSV/GeoJSON) for the technical-report analysis deliverable. |
| NFR-13 | **LLM cost & rate control** | *(New v1.1.)* Cap tokens per request, rate-limit calls, cache identical text, and set a spend ceiling so spam (RISK-3) cannot run up the API bill; fall back to FR-13a when limits are hit. |

---

## 9. Privacy, Legal & Ethical Requirements

- **P1 — Public visibility disclosure.** Tell users clearly what becomes public **before** submitting.
- **P2 — Image privacy.** Photos may show faces/plates/property (A8). MUST support moderation (FR-31); SHOULD consider auto face/plate blurring (COULD); provide a privacy-violation reporting path.
- **P3 — EXIF/location leakage.** ❓ Decide whether uploaded-photo EXIF GPS is stored/exposed. Recommendation: **strip EXIF GPS on upload**; rely on the explicit report location.
- **P4 — Data-protection alignment.** Align with applicable Bangladesh data-protection expectations: consent, purpose limitation, retention, deletion.
- **P5 — Liability disclaimer.** In-product notice that UrbanMend is **not an emergency service** and does not guarantee resolution (RISK-1).
- **P6 — Right to deletion.** Users can request account + PII deletion; public issue records may be retained anonymized/aggregated.
- **P7 — Third-party LLM disclosure.** *(New v1.1.)* Because report text is sent to an external LLM provider (A11), disclose this, avoid sending unnecessary PII in the prompt, and prefer a provider/config that does not train on submitted data.

---

## 10. Success Metrics (KPIs)

- **Adoption:** registered citizens; reports/week; % returning reporters.
- **Report quality:** % reports with valid location + usable photo; % auto-categorized without correction.
- **AI quality:** category accuracy on a held-out set (target ≥ 85%, A10-realistic); **severity agreement** with human judgment; % severities overridden by authorities; clustering precision/recall on duplicates.
- **Operational impact:** median time-to-acknowledge and time-to-resolve, by category and severity; backlog age.
- **Trust/equity:** % issues with a viewable severity explanation; report coverage across neighborhoods (T4).
- **LLM health/cost:** *(new)* API success rate, median latency, fallback-invocation rate, cost per 100 reports.
- **Notification health:** % status changes notified within SLA; delivery/bounce rate.

---

## 11. Missing Requirements Identified (Gaps in the Proposal)

1. **Report vs Issue distinction & clustering mechanics** — undefined in proposal. → §6.1, FR-18.
2. **Authority verification mechanism** — undefined. → FR-2 (admin-provisioned).
3. **Explainability & auditability of triage** — needed for a public-safety system. → FR-15, FR-20, FR-32.
4. **Honest corroboration count** — avoid trivially inflatable "N reporters." → §7, FR-16.
5. **Equity/representation bias** — flagged by the proposal's own citation. → T4.
6. **Geospatial data strategy & POI source** — proximity display + clustering + map need spatial queries and a POI dataset. → A5, NFR-1.
7. **Language handling (Bangla/Banglish)** — central to classification quality. → A1, FR-12, NFR-8.
8. **Privacy of user-submitted photos.** → §9.
9. **Emergency-service boundary & liability.** → RISK-1, P5.
10. **Notification channel realities** (SMS cost, phone verification, deliverability). → A6, FR-27/28.
11. **Issue lifecycle / state machine** beyond "Pending → Resolved." → §6.3.
12. **LLM integration operations** — key management, cost/rate control, fallback, data-privacy of prompts. → A4/A11, FR-13a, NFR-13, P7.
13. **Handling low-confidence AI output** — fallback + human-in-the-loop. → FR-10/11, FR-13a.
14. **Data export for the required technical report.** → NFR-12.

---

## 12. Suggested Improvements (Beyond the Proposal)

- **S1 — Treat classification as a single swappable "AI service" interface.** Hide the specific provider (OpenAI/Claude/Gemini) behind one internal boundary so you can switch models without touching the rest of the app.
- **S2 — Deterministic keyword fallback (FR-13a)** guarantees the demo works even if the API is down — de-risks evaluation day.
- **S3 — "Confirm this issue" (me-too)** instead of forcing duplicate reports — cleaner corroboration count (FR-16), better UX, harder to game.
- **S4 — Public transparency dashboard** (FR-34) — serves the transparency objective; strong demo asset.
- **S5 — Severity-first triage is itself an equity win** (T4): decoupling urgency from report volume avoids starving under-reported poor areas. Worth calling out explicitly in the technical report.
- **S6 — PWA + offline-tolerant submission** (FR-8) — realistic for Bangladeshi connectivity (A2).
- **S7 — Log every human correction** (FR-11) — gives the technical report real before/after accuracy numbers and future prompt-tuning examples, without training a model.
- **S8 — Auto face/plate blurring** (COULD) — privacy-by-design, strong ethics story.

---

## 13. Risks

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|:--:|:--:|-----------|
| RISK-1 | Users treat UrbanMend as an **emergency line**; a life-threatening hazard is reported here instead of 999, response is slow. | Med | **Critical** | Prominent disclaimer (P5); redirect critical categories to emergency guidance; escalation alerts (FR-29). |
| RISK-2 | **AI mis-labels severity**: a dangerous issue marked Low → harm, reputational exposure. | Med | High | Explainable severity (FR-15); no silent suppression (T6); aging visibility (FR-19); authority override (FR-20); human-in-loop (FR-11). |
| RISK-3 | **Gaming / spam floods** inflate the corroboration count or run up LLM cost. | Med | Med | Distinct-reporter counting (T1); rate limits (FR-33); LLM cost caps (NFR-13). *(Lower impact now that count is display-only.)* |
| RISK-4 | **Equity bias** under-serves poor neighborhoods. | Med | Med | Severity-first triage (T4/S5); coverage metrics. *(Reduced by the severity-only decision.)* |
| RISK-5 | **External LLM dependency**: outage, latency, cost spikes, or provider policy change breaks classification. | Med | Med | Keyword fallback (FR-13a); async processing (NFR-3); cost caps (NFR-13); provider hidden behind one interface (S1). |
| RISK-6 | **Privacy incident** from published photos. | Med | High | Moderation (FR-31); EXIF stripping (P3); disclosure (P1); blurring (S8). |
| RISK-7 | **POI reference data** unavailable/stale/licensed → weak proximity context. | Med | Low | Confirm OSM/dataset (A5); admin-managed POIs (FR-30); degrade gracefully (context is display-only). |
| RISK-8 | **Scope creep** toward native apps / real integration / dispatch / re-adding a scoring engine. | High | Med | Non-goals (§2.2) enforced via change control. |
| RISK-9 | **Notification cost/deliverability** (SMS). | Med | Low | Prefer in-app + email; SMS for High-severity only; verify numbers (A6). |
| RISK-10 | **Duplicate clustering errors** — over-merge hides distinct issues, under-merge inflates counts. | Med | Med | Conservative clustering + merge/split tools (FR-25); tunable thresholds. |
| RISK-11 | **Academic timeline** too tight. | Med | Med | Strict MoSCoW; MUST-set is a coherent MVP; LLM + geospatial are the main efforts, scoring engine removed. |
| RISK-12 | **PII sent to a third party** (report text → LLM). | Med | Med | Disclose (P7); minimize PII in prompts; choose no-training provider config; keyword fallback avoids sending text at all when triggered. |

---

## 14. Edge Cases

**Submission**
- GPS denied/unavailable → force manual pin; never allow a locationless report.
- Pin **outside the served city** → warn/block or mark out-of-scope.
- Upload fails mid-submit / connection drops → resumable or queued (FR-8); no data loss.
- Oversized/wrong-type/corrupt/EXIF-rotated image → validate, compress, orient (FR-7).
- Empty/gibberish/emoji-only/very long description → validation + robust prompting.
- Bangla / English / Banglish / mixed script in one report (A1, FR-12).
- Double-tap / retry → idempotency; no accidental duplicate reports.

**Classification & clustering**
- **LLM API times out or returns malformed output** → fall back to keyword rules (FR-13a); retry with backoff; never block the queue.
- **LLM returns a category/severity outside the allowed set** → validate and coerce to `Other`/nearest valid, flag for review.
- One real issue reported by 50 people (mass event) → clustering (FR-18) collapses to one Issue; count shown, severity unaffected.
- Two different issues at the same coordinates (pothole *and* broken light) → must not wrongly merge (RISK-10).
- Same issue reported after a prior resolution → reopen vs new-issue logic (§6.3).
- No nearby POI → proximity context simply absent, not an error.
- Conflicting categories/severities across a cluster's reports → resolve at Issue level (e.g. highest severity wins, shown with rationale).
- Keyword-stuffed report ("danger accident flood") to force High → LLM judges context; fallback keyword hit is capped/annotated, and severity is overridable (FR-20).

**Lifecycle & notifications**
- Report auto-marked duplicate — original reporter still tracks "their" issue (FR-18).
- Issue resolved then recurs → reopen path.
- Citizen deletes account with open reports → issues persist anonymized (P6).
- Notification to an unverified/invalid channel → don't send; don't leak the report's existence.
- Rapid repeated status changes → debounce notifications, don't spam.
- Authority scoped to Sanitation views a Roads issue → RBAC/scoping (FR-3, FR-22).

**Trust & access**
- One person, many accounts/devices → Sybil handling (T3), protects the corroboration count.
- Anonymous vs authenticated reporting (❓Q4) affects tracking and abuse.
- Two authorities edit the same issue concurrently → conflict handling / optimistic locking (design-level).

---

## 15. Open Questions (❓ Require Stakeholder Input Before Freeze)

- **❓Q1 — Category taxonomy.** Is the §6.2 draft correct/complete for the target city?
- **❓Q2 — Severity levels.** Three levels (High/Med/Low), or add a **Critical** band? What distinguishes each level?
- **❓Q3 — POI data source & licensing.** OSM, a government dataset, or admin-entered? Which POI types show as proximity context? (A5)
- **❓Q4 — Anonymous reporting.** Allowed (lower friction) or require an account (better abuse control + tracking)?
- **❓Q5 — Notification channels for the prototype.** In-app only, + email, and/or SMS? SMS budget? (A6)
- **❓Q6 — EXIF/location privacy default.** Strip photo GPS on upload (recommended) or retain? (P3)
- **❓Q7 — Public visibility granularity.** All reports public, exact location fuzzed, or some private? (P1)
- **❓Q8 — Definition of "resolved."** Authority self-attestation, or citizen confirmation required to close?
- **❓Q9 — LLM provider & data policy.** Which provider (OpenAI/Claude/Gemini)? Confirm a configuration that **does not train on submitted data** and fits budget (A4, A11, P7). *(Replaces v1.0's training-dataset question — no dataset is needed.)*
- **❓Q10 — Accuracy acceptance bar.** Is ≥ 85% top-1 category accuracy (plus a severity-agreement target) acceptable for the academic evaluation?

---

## 16. Confirmed Planning Decisions (Inputs to This PRD)

1. **Deployment context:** Single city, Bangladesh (→ A1, A2, localization).
2. **AI approach:** **Hosted LLM API** (OpenAI/Claude/Gemini) for categorization + severity, with a **keyword fallback**; **no custom model trained or hosted** (→ A4, A11, FR-10–13a, NFR-13, S1).
3. **Triage model:** **Severity label only** — the weighted numeric priority score is **removed**. Frequency (corroboration count) and proximity are **display-only context**, not score inputs. Narrowing of the proposal's 3-signal scoring is **accepted** (→ §2.2, §5.4, §18).
4. **Authorities:** Simulated / admin-provisioned roles (→ A3, FR-2).
5. **Technology:** Architect to recommend the best-fit stack; **geospatial-first is still required** for proximity context, clustering, and the map (→ NFR-1). Specific stack deferred to the architecture document.

---

## 17. Traceability — Proposal → PRD

| Proposal objective / FR | Addressed by | Notes |
|---|---|---|
| Report with geolocation + image | FR-5, FR-6, FR-7 | |
| AI auto-categorization (NLP) | FR-10, FR-12, FR-13 | Now via hosted LLM API, not a trained model |
| Priority by keywords / frequency / proximity | FR-14, FR-15 (keywords→severity); FR-16 (frequency, display-only); FR-17 (proximity, display-only) | **Narrowed:** no computed score; see §18 |
| Authority dashboard, hotspots, status | FR-22, FR-23, FR-24, §6.3 | Queue sorts by severity |
| Citizen transparency / tracking | FR-1, FR-27, FR-34, P1 | |
| Notifications on status change | FR-27, FR-28 | |
| User authentication (citizen + authority) | FR-1, FR-2, FR-3 | |
| Technical report (AI logic/accuracy) | KPIs §10, NFR-12, FR-11 | Contribution reframed around LLM classification + severity triage |
| User manual (citizens + authorities) | Downstream deliverable | Out of PRD scope |

---

## 18. Deviation Notice — Proposal vs This PRD

Two deliberate deviations from `PROJECT PROPOSAL.pdf`, recorded for your supervisor's awareness:

1. **AI implementation.** The proposal implies a self-built NLP/ML classifier. This PRD uses a **hosted LLM API**. *Rationale:* superior Bangla/Banglish handling, no training data or model-hosting burden, and a stronger, more reliable demo (A4, RISK-5 mitigations). The academic contribution shifts from "we trained a model" to "we designed and evaluated an LLM-based civic-triage pipeline with a deterministic fallback."
2. **Prioritization mechanism.** The proposal specifies a **priority score from three signals** (keywords + frequency + proximity). This PRD replaces the score with a **severity label** and keeps frequency + proximity as **display-only context**. *Rationale:* transparency, simplicity, and reduced equity bias (T4/S5) — at the cost of narrowing the proposal's headline scoring contribution. **This narrowing has been explicitly accepted during planning** (§16.3). If the supervisor expects the full three-signal score, revisit §5.4.

---

*End of `docs/01-prd.md` (v1.1). Per instruction, no further documents have been created. Resolve the §15 open questions to freeze this PRD before proceeding to architecture/design.*
