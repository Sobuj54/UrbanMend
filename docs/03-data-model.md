# UrbanMend — Domain Data Model

> The business domain of UrbanMend: entities, relationships, rules, and lifecycles — independent of any database technology.

| | |
|---|---|
| **Document** | `docs/03-data-model.md` |
| **Version** | 1.0 (Draft for review) |
| **Status** | Planning phase — pending stakeholder sign-off |
| **Author role** | Principal Backend Architect |
| **Date** | 2026-07-22 |
| **Source of truth** | `docs/01-prd.md` (PRD v1.1, approved) + `docs/02-architecture.md` (SDD v1.0, approved) |
| **Scope of this doc** | **Business domain model only.** No schema, columns, keys, indexes, or SQL. |
| **Downstream docs** | `04-api-specification.md`, `05-project-plan.md` |

### Ground rules
- The PRD and Architecture are the **single source of truth.** No new features; no changed requirements. Every entity, rule, and lifecycle traces to a PRD `FR-x`/`NFR-x` or an Architecture section.
- This document describes the **domain**, not the **database.** "Key attributes" are conceptual, not fields. Persistence detail (types, indexes, PostGIS specifics, foreign keys) is deferred to the schema design.
- Purely technical mechanisms (transactional outbox, sessions, cache, queue) are **out of scope** here — they are implementation, not domain (Architecture §7/§11).

---

## Overview

### Purpose of the data model
To define the **language and structure of the business domain** so that every downstream artifact (API contracts, database schema, application logic) shares one consistent, unambiguous vocabulary. It answers: *what things exist in UrbanMend, what they mean, how they relate, what rules govern them, and how they change over their lifetime* — without committing to how they are stored.

Concretely, the model must faithfully express the PRD's most important structural decisions:
1. The separation of a **Report** (one citizen submission) from an **Issue** (a cluster representing one real-world problem).
2. That **severity, status, and assignment are properties of the Issue**, not the Report.
3. That **corroboration** (frequency) and **proximity** are contextual/derived — they inform humans but never compute a score (there is no score; PRD §5.4).
4. That every integrity-relevant change is **auditable** and, where citizen-facing, **notified**.

### Scope

**In scope:** the citizen-reporting and authority-triage domain — users and roles, reports, media, issues and clustering, categories, severity, confirmations, comments, points of interest, notifications and preferences, status history, audit records, and the admin-managed reference/configuration data the PRD requires.

**Out of scope:** database schema and physical design; authentication mechanics (OTP, sessions, tokens); infrastructure concepts (queues, cache, outbox); UI/UX; the frontend; anything marked out-of-scope in PRD §2.2 (native apps, dispatch logistics, financial integration, real municipal integration, multi-tenant/multi-city, numeric scoring, custom ML models).

---

## Core Entities

The domain comprises the following entities, grouped by concern:

- **People & Access:** User (Citizen / Authority / Admin)
- **Reporting:** Report, Media, Confirmation, Comment
- **Triage:** Issue, Category, Severity *(value concept)*
- **Geography:** Point of Interest, City Boundary *(reference)*
- **Engagement:** Notification, Notification Preference
- **Integrity & History:** Status Event, Audit Event
- **Reference / Configuration:** Severity Keyword, Clustering Rule

---

### 1. User

**Purpose.** Represents any person who interacts with UrbanMend in an authenticated capacity. A single entity carries a **role** that determines capabilities: **Citizen**, **Authority**, or **Admin** (PRD §4.2).

**Responsibilities.**
- Own the reports and confirmations a citizen creates.
- Carry the role and, for Authorities, the **category scope** that constrains what they may see and act on (FR-2).
- Anchor notification delivery and preferences.
- Serve as the **actor** on audit and status records.

**Key attributes (high level).** Identity/contact (email and/or phone), verification state, role, authority category-scope (Authorities only), account status, and — for reporters — a **trust signal** used by honest-corroboration counting (T1/T2; see review §"Missing entities").

**Relationships.**
- Citizen **1 → many** Report (authorship).
- Citizen **1 → many** Confirmation.
- User **1 → many** Comment.
- Authority **many ↔ many** Category (scope).
- Authority **1 → many** Issue (as assignee, optional).
- User **1 → 1** Notification Preference; **1 → many** Notification.
- User **1 → many** Audit Event (as actor).

**Lifecycle.** `Registered (unverified) → Verified → Active → Suspended → Deprovisioned/Deleted (anonymized)`. Authority status is reached only via an Admin grant (FR-2). Detailed transitions in *Entity Lifecycle*.

---

### 2. Report

**Purpose.** A **single citizen submission** describing one observed public issue at one place and time (FR-5). It is the raw, immutable-in-intent unit of crowd-sourced input.

**Responsibilities.**
- Capture what the citizen observed: description, media, and an authoritative location.
- Hold the **classification result** produced asynchronously (category, severity signal, confidence, and whether it came from the LLM or the keyword fallback) — Architecture §4/§6.
- Contribute to exactly one **Issue** after triage.

**Key attributes (high level).** Author (Citizen), free-text description, capture time, authoritative **location** (coordinate + reverse-geocoded address), classification outcome (category, severity signal, confidence, classification **source** = `llm` | `fallback`), processing state, and moderation state.

**Relationships.**
- Report **many → 1** Citizen (author).
- Report **many → 1** Issue (after triage; none while processing).
- Report **1 → many** Media.
- Report **many → 1** Category (via its classification).

**Lifecycle.** `Draft (optional, FR-8) → Submitted → Processing → Triaged (attached to an Issue) → [Duplicate-linked]`; plus a moderation branch `→ Hidden/Removed` (FR-31) and a privacy branch `→ Anonymized` (P6). **The Report never carries resolution status** — that belongs to its Issue.

---

### 3. Issue

**Purpose.** A **cluster of one or more Reports** that represent the same real-world problem (FR-18). The Issue is the **unit of work** authorities triage, prioritize, and resolve, and the unit citizens ultimately track.

**Responsibilities.**
- Aggregate its member Reports and expose the **corroboration count** (distinct reporters, FR-16).
- Own the **severity** (High/Medium/Low), the **workflow status** (PRD §6.3), and the **assignment** to an authority (FR-24) — none of which live on individual Reports.
- Preserve both the **computed severity** and any **authority override** with its reason (FR-20).
- Carry **display-only context**: nearby points of interest (FR-17).

**Key attributes (high level).** Primary Category, computed severity + its rationale (FR-15), optional overridden severity + actor + reason, workflow status, assignee (optional), corroboration count (derived), representative location, open/age timestamps, and proximity context (derived).

**Relationships.**
- Issue **1 → many** Report (members).
- Issue **1 → many** Confirmation.
- Issue **1 → many** Comment.
- Issue **1 → many** Status Event (history).
- Issue **many → 1** Category.
- Issue **many → 0..1** Authority (assignee).
- Issue **many ↔ many** Point of Interest (nearby, **derived/contextual**, display-only).
- Issue **many → 0..1** Issue (a Duplicate links to the surviving Issue).

**Lifecycle.** The PRD §6.3 state machine: `Submitted → Triaged → Acknowledged → In Progress → Resolved → Closed`, with branches `Rejected / Duplicate / Insufficient Info`, and an optional reopen from Resolved/Closed. Detailed in *Entity Lifecycle*.

---

### 4. Media

**Purpose.** A photo (and its derived thumbnail) attached to a Report as visual evidence (FR-7).

**Responsibilities.**
- Reference the stored image and its processed derivatives.
- Track processing state (compression, thumbnailing, **EXIF/GPS stripping** — P3).

**Key attributes (high level).** Owning Report, storage reference(s), processing state, and content-safety/moderation state (FR-31, S8-ready).

**Relationships.** Media **many → 1** Report.

**Lifecycle.** `Uploaded → Processing → Ready` with `→ Failed (retry)` and `→ Removed (moderated)` branches. A Report can be usable before its Media is `Ready` (Architecture §4.1).

---

### 5. Category

**Purpose.** A node in the controlled **classification taxonomy** (PRD §6.2, e.g. Roads & Transport, Street Lighting, Water & Drainage, Sanitation & Waste, Electrical Hazards, Public Structures, Other).

**Responsibilities.**
- Constrain classification outputs to a known set.
- Define authority **scope** and dashboard filtering.
- Anchor per-category configuration (clustering rules, keyword lists).

**Key attributes (high level).** Name (bilingual label), active state, and default handling hints.

**Relationships.**
- Category **1 → many** Report / Issue.
- Category **many ↔ many** Authority (scope).
- Category **1 → many** Clustering Rule / Severity Keyword.

**Lifecycle.** `Active → Retired` (retired categories keep historical references but accept no new classification). Managed by Admin (FR-30).

---

### 6. Severity *(value concept, not an independent record)*

**Purpose.** The triage signal that ranks work: **High / Medium / Low** (FR-14; a possible "Critical" band is PRD ❓Q2).

**Modeling note.** Severity is a **value** carried by a Report (as a classification signal) and by an Issue (as its authoritative, possibly-overridden level). It is not an entity with its own lifecycle; it changes only as an attribute of the Report/Issue that owns it and always with rationale (FR-15) and, when overridden, an audited reason (FR-20).

---

### 7. Confirmation ("me-too")

**Purpose.** A citizen's assertion that an existing Issue affects them too (S3), providing **honest corroboration** without forcing a duplicate Report.

**Responsibilities.**
- Represent one **distinct reporter's** endorsement of an Issue.
- Feed the corroboration count (FR-16) under the distinct-reporter rule (T1).

**Key attributes (high level).** Confirming Citizen, target Issue, timestamp.

**Relationships.** Confirmation **many → 1** Citizen; **many → 1** Issue.

**Lifecycle.** `Created` (effectively immutable; at most one per citizen per Issue). Optionally revocable — see ❓Open Questions.

---

### 8. Comment

**Purpose.** Additional information or an update attached to an Issue — either a **public update** from an authority or a citizen note (FR-24, role matrix "Comment / add info").

**Responsibilities.**
- Carry human commentary and public status updates.
- Distinguish **public** comments (visible to citizens) from **internal notes** (authority/admin only) — FR-24.

**Key attributes (high level).** Author, target Issue, body, visibility (public/internal), timestamp.

**Relationships.** Comment **many → 1** Issue; **many → 1** User (author).

**Lifecycle.** `Created → [Edited] → [Removed (moderated)]`.

---

### 9. Point of Interest (POI)

**Purpose.** A seeded **sensitive landmark** (hospital, school, highway, market) used solely to display **proximity context** on an Issue (FR-17, A5).

**Responsibilities.**
- Provide reference geography for nearest-landmark context.
- Remain **display-only** — POIs never influence severity or ranking (PRD §5.4).

**Key attributes (high level).** Name, type, location, source, active state.

**Relationships.** POI **many ↔ many** Issue (nearby; a **derived/contextual** association, not an ownership link).

**Lifecycle.** `Active → Retired`. Admin-managed reference data (FR-30).

---

### 10. Notification

**Purpose.** A message informing a user of a relevant change — primarily a status transition on their tracked Issue (FR-27), optionally an authority alert (FR-29).

**Responsibilities.**
- Represent one intended message and its delivery outcome across channels (in-app/email/SMS).
- Respect verified channels and severity-gated SMS (RISK-9).

**Key attributes (high level).** Recipient, subject reference (usually an Issue), channel(s), content, delivery state/outcome, timestamp.

**Relationships.** Notification **many → 1** User (recipient); **many → 0..1** Issue (subject).

**Lifecycle.** `Pending → Sent → Delivered | Failed`. Rapid successive changes are debounced (edge case).

---

### 11. Notification Preference

**Purpose.** A user's choice of channels and opt-outs (FR-28).

**Key attributes (high level).** Owning User, per-channel enablement, opt-out flags (respecting transactional vs marketing).

**Relationships.** Notification Preference **1 → 1** User.

**Lifecycle.** Created with the account; mutable by its owner.

---

### 12. Status Event

**Purpose.** An immutable record of a single **Issue** state transition (PRD §6.3, FR-24), forming the issue's history.

**Responsibilities.**
- Record who changed the status, from what to what, when, and why (reason where applicable).
- Serve as the trigger source for notifications and analytics (time-to-resolution).

**Key attributes (high level).** Target Issue, previous status, new status, actor, reason (optional), timestamp.

**Relationships.** Status Event **many → 1** Issue; **many → 1** User (actor).

**Lifecycle.** `Created` only — append-only, never modified.

---

### 13. Audit Event

**Purpose.** An **append-only integrity record** of security- and integrity-relevant actions (FR-32, NFR-10): auth events, role grants, status changes, **severity overrides**, moderation, and reference-data changes.

**Responsibilities.**
- Preserve an immutable trail with actor, action, target, before/after, and timestamp.
- Be queryable by Admin (and by an Authority for their own actions).

**Key attributes (high level).** Actor, action type, target reference (any entity), before/after snapshot, timestamp.

**Relationships.** Audit Event **many → 1** User (actor); references any entity polymorphically.

**Lifecycle.** `Created` only — strictly immutable (no update/delete).

---

### 14. Severity Keyword *(reference/configuration)*

**Purpose.** A bilingual (Bangla/English) keyword mapped to a severity level, powering the **deterministic fallback** classifier and the severity rationale (FR-13a, FR-14, FR-30).

**Key attributes (high level).** Term, language, associated severity, optional category, active state.

**Relationships.** Severity Keyword **many → 0..1** Category. Admin-managed (FR-30).

**Lifecycle.** `Active → Retired`; fully admin-editable.

---

### 15. Clustering Rule *(reference/configuration)*

**Purpose.** The per-category **radius and time-window** that govern whether a new Report joins an existing Issue (FR-18, Architecture §4.3, ASSUMP-4).

**Key attributes (high level).** Category, spatial radius, time window, active state.

**Relationships.** Clustering Rule **many → 1** Category. Admin-managed (FR-30).

**Lifecycle.** `Active`, editable by Admin; changes affect only future clustering decisions.

---

### 16. City Boundary *(reference)*

**Purpose.** The served city's geographic boundary, used to enforce the "report outside the served city" rule (Architecture §9, ASSUMP-6). Single city for now (PRD §2.2 non-goal: multi-city).

**Key attributes (high level).** City name, boundary geometry, active state.

**Relationships.** Conceptually **1 → many** Report/Issue (all belong to the one served city; latent for future multi-city, ASSUMP-8).

**Lifecycle.** Effectively static reference data.

---

## Entity Relationships

### Relationship map (conceptual)

```
                 ┌───────────────┐
                 │     User      │  role: Citizen | Authority | Admin
                 └──┬────┬────┬──┘
       authors      │    │    │  assignee (Authority)
        ┌───────────┘    │    └──────────────────────────┐
        ▼                │ scope (Authority)              ▼
   ┌─────────┐           │ many↔many                 ┌─────────┐
   │ Report  │──many→1──▶│                           │  Issue  │◀── assignee 0..1
   └──┬───┬──┘        ┌──▼──────┐   member of        └──┬───┬──┘
      │   │           │Category │◀───many→1───────────  │   │
   1→*│   │many→1     └──┬───┬──┘                        │   │1→*
      ▼   └─────────────┘   │ config                    │   ▼
  ┌───────┐                 │                            │ ┌────────────┐
  │ Media │                 │1→*                         │ │Status Event│ (append-only)
  └───────┘        ┌────────▼─────────┐                  │ └────────────┘
                   │ Clustering Rule / │                  │
                   │ Severity Keyword  │                  │1→*
                   └──────────────────┘                   ▼
   ┌─────────────┐     confirms         ┌─────────┐   ┌─────────┐
   │  Citizen    │──many→1──────────────▶│  Issue  │   │ Comment │ (public|internal)
   └─────────────┘   (Confirmation)      └────┬────┘   └─────────┘
                                              │ many↔many (derived, display-only)
                                              ▼
                                        ┌───────────┐
                                        │    POI    │
                                        └───────────┘

  User 1→1 Notification Preference ;  User 1→* Notification →(0..1) Issue
  User 1→* Audit Event (actor) ; Audit Event → any entity (polymorphic, append-only)
  City Boundary 1→* Report/Issue (single served city; latent for multi-city)
```

### Cardinalities explained

**One-to-one (1:1)**
- **User ↔ Notification Preference** — each user has exactly one preference profile; it exists only in the context of that user.

**One-to-many (1:N)**
- **Citizen → Report** — a citizen files many reports; each report has exactly one author.
- **Issue → Report** — an issue clusters many reports; each report belongs to at most one issue (none while processing, exactly one after triage).
- **Report → Media** — a report may carry several photos; each photo belongs to one report.
- **Issue → Status Event / Confirmation / Comment** — an issue accrues many of each over its life.
- **Category → Report / Issue / Clustering Rule / Severity Keyword** — a category classifies/configures many records.
- **User (Authority) → Issue** — an authority may be assigned many issues; an issue has at most one assignee.
- **User → Notification / Audit Event** — many per user.
- **Issue → Issue** (self, N:1) — many duplicate issues can point to one surviving issue.

**Many-to-many (M:N)**
- **Authority ↔ Category** — an authority is scoped to one or more categories; a category can be served by several authorities (FR-2). Resolved via an association concept (authority scope).
- **Issue ↔ POI** — an issue may be near several landmarks and a landmark near several issues. This is a **derived, display-only** association computed from geography (FR-17); it carries no ownership and never affects severity.

---

## Business Rules

Grouped by area; each traces to the PRD/Architecture.

**Reporting**
- BR-1. A Report is authored by **exactly one** Citizen (subject to anonymous-reporting ❓Q4). *(FR-5)*
- BR-2. A Report **must** have an authoritative location; it cannot be submitted without one. *(FR-6)*
- BR-3. A Report must include **at least one of** {photo, adequate description}. *(FR-5)*
- BR-4. A Report's authoritative location is the **explicit report coordinate**, never photo EXIF; EXIF/GPS is stripped by default. *(P3)*
- BR-5. Duplicate submissions from one action (double-tap/retry) resolve to a **single** Report (idempotency). *(Edge cases)*
- BR-6. A Report belongs to **at most one** Issue at any time. *(FR-18)*

**Classification & severity**
- BR-7. Every Report receives a **Category from the controlled taxonomy**; an out-of-set result is coerced to `Other` and flagged. *(FR-10, edge case)*
- BR-8. Every Report receives a **severity signal** ∈ {High, Medium, Low} (Critical pending ❓Q2). *(FR-14)*
- BR-9. Classification runs **asynchronously**; a Report validly exists before it is classified. *(NFR-3)*
- BR-10. If the LLM is unavailable/over-budget, the **keyword fallback** assigns category and severity; the **source** is recorded. *(FR-13a)*
- BR-11. An Issue's severity equals the **highest** severity among its member Reports, unless an authority override exists. *(§4.4)*
- BR-12. Every severity value carries a **human-readable rationale**. *(FR-15)*

**Issues, clustering & lifecycle**
- BR-13. A new Report joins an existing **open** Issue only if it matches on **category + proximity + time window** per the applicable Clustering Rule; otherwise a new Issue is created. *(FR-18, §4.3)*
- BR-14. An Issue must contain **at least one** Report at all times (a split must leave ≥1 Report on each side). *(§4.3, FR-25)*
- BR-15. Only **Authorities/Admins** may advance an Issue's status past `Triaged`. *(§6.3)*
- BR-16. Status transitions must follow the defined **state machine**; invalid transitions are rejected. *(§6.3)*
- BR-17. A **Duplicate** Issue links to the surviving Issue; the original reporter still tracks their own Report. *(FR-18, §6.3)*
- BR-18. **Merge/split** of clusters may be performed only by an Authority/Admin. *(FR-25)*
- BR-19. An Issue may be **reopened** from Resolved/Closed only with a reason (SHOULD). *(§6.3)*

**Severity override**
- BR-20. Only an **Authority/Admin** may override an Issue's severity, and a **reason is mandatory**. *(FR-20)*
- BR-21. An override **never overwrites** the computed severity; both are retained and shown. *(FR-20, §4.4)*

**Corroboration & proximity**
- BR-22. The corroboration count reflects **distinct trustworthy reporters**, not raw report/confirmation volume. *(FR-16, T1)*
- BR-23. A Citizen may confirm ("me-too") a given Issue **at most once**. *(T1)*
- BR-24. Corroboration and proximity are **display-only**; neither changes severity or ranking. *(§5.4, FR-16/17)*

**Access & ownership**
- BR-25. An **Authority role can be granted only by an Admin**, and the grant is audited. *(FR-2, FR-32)*
- BR-26. An Authority may view/act on Issues **only within their category scope**. *(FR-2, FR-3, FR-22)*
- BR-27. **Authorization is enforced server-side** on every mutating and sensitive-read action. *(FR-3)*

**History, notification & integrity**
- BR-28. Every status transition and severity override generates an **Audit Event**. *(FR-32)*
- BR-29. Every citizen-facing status change generates a **Notification** within the stated SLA. *(FR-27)*
- BR-30. Notifications are sent **only to verified channels**; SMS is reserved for **High** severity. *(Edge case, RISK-9)*
- BR-31. **Audit Events and Status Events are append-only** and immutable. *(FR-32, NFR-10)*

**Privacy & moderation**
- BR-32. Content that is abusive/illegal/privacy-violating may be **hidden/removed by moderation**, with reason logged. *(FR-31)*
- BR-33. On account deletion, the user's **PII is anonymized** while public Issue records are retained. *(P6)*

**Reference data**
- BR-34. Categories, POIs, Severity Keywords, and Clustering Rules are **admin-managed reference data**, not hard-coded. *(FR-30, NFR-11)*
- BR-35. A Report/Issue location outside the **served city boundary** is flagged/blocked. *(§9, edge case)*

---

## Entity Lifecycle

### Report

```
[Draft]* ──▶ Submitted ──▶ Processing ──▶ Triaged ──▶ (member of an Issue)
                                │                └──▶ Duplicate-linked
                                └──(moderation)──▶ Hidden / Removed
                                └──(privacy)─────▶ Anonymized
* Draft is optional (FR-8, SHOULD).
```
The Report **never** holds resolution status. Once `Triaged`, its progress is expressed through its Issue.

### Issue (authoritative workflow — PRD §6.3)

```
Submitted ──▶ Triaged ──▶ Acknowledged ──▶ In Progress ──▶ Resolved ──▶ Closed
                 │                                              ▲
                 ├──▶ Rejected                                  │ reopen (with reason, SHOULD)
                 ├──▶ Duplicate ──(links to surviving Issue)    │
                 └──▶ Insufficient Info                         │
Resolved/Closed ──────────────────────────────────────────────┘
```
- Entry to `Triaged` is automatic (post-classification/clustering).
- All forward transitions past `Triaged` require an Authority/Admin (BR-15) and emit a Status Event (BR-28) + Notification (BR-29).

### User

```
Registered (unverified) ──▶ Verified ──▶ Active ──▶ Suspended ──▶ Deprovisioned
                                                        │
                                                        └──▶ Deleted (PII anonymized)
Authority status: reachable only via Admin grant (BR-25).
```

### Media

```
Uploaded ──▶ Processing ──▶ Ready
                 │             └──(moderation)──▶ Removed
                 └──▶ Failed ──(retry)──▶ Processing
```

### Notification

```
Pending ──▶ Sent ──▶ Delivered
              └────▶ Failed
(rapid successive changes are debounced before Pending)
```

### Confirmation / Comment / Status Event / Audit Event
- **Confirmation:** `Created` (immutable; one per citizen per Issue).
- **Comment:** `Created → [Edited] → [Removed]`.
- **Status Event / Audit Event:** `Created` only — append-only, immutable.

---

## Ownership & Permissions

"Owner" = the role accountable for the entity's existence/content. CRUD is at the domain level (subject to authority category-scope and server-side enforcement, BR-26/27). Legend: **C**reate / **R**ead / **U**pdate / **D**elete; ✅ full, ⚙️ conditional (own/scope/reason), — none.

| Entity | Owner | Anonymous | Citizen | Authority | Admin |
|--------|-------|:--:|:--:|:--:|:--:|
| User (own account) | The User | — | CRUD⚙️ (own) | RU⚙️ (own) | CRUD (any; grants Authority) |
| Report | Authoring Citizen | R⚙️ (public view, ❓Q7) | C, R (own+public), U⚙️ (pre-triage/own) | R⚙️ (scope), U⚙️ (re-categorize) | CRUD⚙️ (moderation) |
| Media | Authoring Citizen | R⚙️ (public) | C, R (own), D⚙️ (pre-triage) | R⚙️ | RU D⚙️ (moderation) |
| Issue | System / assigned Authority | R⚙️ (public) | R⚙️ (own/public) | R⚙️ (scope), U (status/assign/override), merge/split | CRUD⚙️ |
| Confirmation | Confirming Citizen | — | C, R, (D if revocable ❓) | R⚙️ | R, D⚙️ |
| Comment | Author | R⚙️ (public only) | C, R (public), U⚙️ (own) | C (public/internal), R, U⚙️ | CRUD |
| Category | Admin | R | R | R | CRUD |
| POI | Admin | R⚙️ | R | R | CRUD |
| Notification | Recipient | — | R⚙️ (own) | R⚙️ (own) | R |
| Notification Preference | The User | — | RU⚙️ (own) | RU⚙️ (own) | R |
| Status Event | System | — | R⚙️ (public issue history) | R⚙️ (scope) | R |
| Audit Event | System | — | — | R⚙️ (own actions) | R |
| Severity Keyword | Admin | — | — | — | CRUD |
| Clustering Rule | Admin | — | — | — | CRUD |
| City Boundary | Admin | R | R | R | CRUD |

*Notes:* Status Events and Audit Events are **never** updatable/deletable by anyone (BR-31); "D" is absent by design. Public visibility (Anonymous/Citizen R on Reports/Issues) depends on PRD ❓Q7.

---

## Domain Constraints
*(Technology-independent invariants the model must always uphold.)*

- **C-1.** Severity is a bounded set {High, Medium, Low} (+ Critical only if ❓Q2 confirms). *(FR-14)*
- **C-2.** Category values are drawn from the controlled taxonomy; no free-form categories. *(FR-10, §6.2)*
- **C-3.** Every Report has exactly one authoritative geographic location. *(FR-6)*
- **C-4.** An Issue always has ≥ 1 member Report. *(§4.3)*
- **C-5.** A Report is attached to at most one Issue at a time. *(FR-18)*
- **C-6.** Corroboration counts distinct reporters; one Citizen contributes at most once per Issue. *(FR-16, BR-23)*
- **C-7.** Issue status changes must respect the state machine (§6.3); no skipping into invalid states.
- **C-8.** Both computed and overridden severity coexist; neither destroys the other. *(FR-20)*
- **C-9.** Audit and Status histories are append-only and immutable. *(FR-32, NFR-10)*
- **C-10.** Proximity/POI associations are derived and display-only; they can never be an input to severity or ordering. *(§5.4)*
- **C-11.** A location outside the served city boundary is not accepted as an in-scope report. *(§9)*
- **C-12.** A classification always records its source (`llm` | `fallback`). *(FR-13a)*
- **C-13.** Reference/configuration data (taxonomy, POIs, keywords, clustering rules) is mutable only by Admin. *(FR-30)*
- **C-14.** Deleting a user must not orphan or destroy public Issue history; it anonymizes instead. *(P6)*

---

## Future Extensions
*(How the model can evolve without a major redesign — all currently out of scope per PRD §2.2.)*

- **Multi-city / multi-tenant.** City Boundary already exists as latent reference (ASSUMP-8); introducing a first-class `City`/`Tenant` scope on Users, Reports, and Issues is additive, not structural.
- **Department as a first-class entity.** Today authority scope is modeled directly against Categories. A `Department` grouping (Department ↔ Category, Authority → Department) can be layered in without altering existing relationships.
- **Reporter reputation/trust as an entity.** The trust *signal* on User (T2) can graduate into a richer `ReporterTrust` concept if anti-abuse needs grow — without touching Reports/Issues.
- **Return of a numeric priority score.** If the deviation in PRD §18 is ever reversed, a `PriorityScore` value could attach to Issue alongside severity, reusing the existing corroboration/proximity signals — no redesign.
- **Automatic image redaction.** Media already models processing/moderation states, so face/plate blurring (S8) is a new processing step, not a new relationship.
- **Vision-assisted classification.** Media is already linked to Report; feeding it into classification (FR-13) adds an input, not an entity.
- **Recurrence tracking.** The Issue self-reference (Duplicate link) generalizes to a "recurs-from" link for issues that reappear after resolution.
- **Public transparency views / open data.** Export and public read models (NFR-12, FR-34) can be built as read projections over the same entities.

---

## Assumptions
*(Recorded, not guessed — where the source docs leave a modeling choice open.)*

- **DM-A1.** Reporter **trust** is a *signal/attribute* on User for now, not a standalone entity (candidate for extraction later). *(T1/T2)*
- **DM-A2.** **Comments attach to the Issue** (the shared, tracked unit), with a public/internal visibility flag, rather than to individual Reports. *(FR-24)*
- **DM-A3.** Authority scope is modeled against **Category** (the PRD's "category/department" treated as category scope); a Department entity is deferred. *(FR-2)*
- **DM-A4.** **Confirmations are single-per-citizen-per-issue** and, by default, non-revocable (see Open Questions).
- **DM-A5.** A single **served city** exists; City Boundary is reference data and all records implicitly belong to it. *(PRD §2.2, ASSUMP-6/8)*
- **DM-A6.** The **classification result** (category, severity signal, confidence, source, model/version) is modeled as attributes of the Report, not as a separate `Classification` entity. *(Architecture §4/§6)*
- **DM-A7.** **POI proximity** is a derived association computed at triage and refreshable; it is not a stored ownership relationship. *(FR-17)*
- **DM-A8.** Public visibility of Reports/Issues to anonymous users is assumed **partial/public** pending ❓Q7; the permission table marks these conditional.

---

## Open Questions
*(Modeling decisions blocked on PRD-level answers or needing product input. PRD open questions restated where they affect the model.)*

- **DM-Q1.** Does severity include a **Critical** band, or only High/Medium/Low? *(PRD ❓Q2 — affects C-1, BR-8)*
- **DM-Q2.** Is **anonymous reporting** permitted? If so, how is authorship/tracking represented for an anonymous Report? *(PRD ❓Q4 — affects BR-1, Report ownership)*
- **DM-Q3.** What is the **public visibility** granularity of Reports/Issues (fully public, location-fuzzed, some private)? *(PRD ❓Q7 — affects permission table, DM-A8)*
- **DM-Q4.** What defines **"Resolved"** — authority self-attestation or citizen confirmation? *(PRD ❓Q8 — affects Issue lifecycle transition into Resolved)*
- **DM-Q5.** Are **Confirmations revocable** (citizen withdraws a "me-too")? *(affects DM-A4, corroboration count)*
- **DM-Q6.** Should **Department** be a first-class entity now, or is Category scope sufficient for the prototype? *(affects DM-A3)*
- **DM-Q7.** When an Issue is **split/merged**, how are member Reports and their Confirmations re-attributed? *(operational rule for BR-14/18)*
- **DM-Q8.** On **reopen**, is a new Issue created or the same Issue reactivated (and how does that affect corroboration and history)? *(refines BR-19)*

---

## Peer Architecture Review
*(A second Principal Backend Architect reviewing this domain model. Findings are advisory; none change PRD/Architecture requirements — they harden the model.)*

### Missing / under-modeled entities
1. **Department** is absent as a first-class entity (DM-A3). Category-scope works for a single city, but FR-2 explicitly says "categories/**departments**." If real organizational routing ever matters, retrofitting is easy (Future Extensions) — acceptable to defer, but call it out at sign-off.
2. **Reporter trust** is only a signal (DM-A1). Since honest corroboration (T1/T2) is a real anti-abuse mechanism, consider whether the *inputs* to trust (verification level, report history) need to be explicit enough to be auditable.
3. **No `Classification` entity** (DM-A6). Folding it into Report is the right call for simplicity, but if you later need to keep a **history** of re-classifications (human corrections, FR-11) for the accuracy KPI, a lightweight classification-history record may be warranted. Today the Audit Event covers the trail.
4. **Attachment generality.** Media is image-only (correct per FR-7). Fine now; noted only so no one assumes documents/video are modeled.

### Weak / ambiguous relationships
5. **Issue ↔ POI** is many-to-many and *derived*. This is correct but easy to misimplement as a stored, authoritative link that accidentally influences ranking — the model must keep it strictly display-only (C-10). Flag prominently for the schema/API docs.
6. **Comment attachment** (DM-A2) — attaching to Issue is clean, but a citizen "adding info to *their* report" (FR-11) that later merges into a multi-report Issue could feel like their note moved. Confirm the UX expectation (DM-Q7 is adjacent).
7. **Report → Issue** transitions from 0..1 to exactly 1 across the processing boundary. Ensure consumers never assume a Report always has an Issue (it doesn't, while `Processing`).

### Missing / implicit business rules worth making explicit
8. **Split/merge re-attribution** (DM-Q7) has no rule yet — what happens to Confirmations and member Reports on split is undefined and directly affects the corroboration count (a public-facing number).
9. **Reopen semantics** (DM-Q8) — new vs reactivated Issue changes history and metrics; currently unstated.
10. **Confirmation revocation** (DM-Q5) — if allowed, corroboration counts become mutable downward; if not, state it as an invariant.
11. **Anonymous authorship** (DM-Q2) — BR-1 assumes one Citizen; the model needs an explicit answer or the ownership/permission rows for Report are underspecified.

### Potential scalability concerns
12. **Audit + Status Event growth.** Append-only histories grow unbounded. At A7 scale this is a non-issue, but a retention/archival policy should be named before any real deployment (Future work, not prototype-blocking).
13. **Corroboration count as derived vs materialized.** Recomputing distinct-reporter counts on every read won't scale to hot issues (mass events). The *domain* treats it as derived; the *schema* will likely need to materialize it — flag for doc 03-schema so the derivation rule (BR-22) and its cached form stay consistent.
14. **Clustering hot-spots.** A single real event (flooding) can pull thousands of Reports into one Issue, making that Issue's aggregates heavy. The model is correct; implementations must page member Reports and cap live recomputation (echoes Architecture §14).
15. **Notification fan-out.** A status change on a massively-corroborated Issue notifies many citizens at once. The domain is fine; delivery must batch (implementation concern, Architecture §7.2).

### Suggestions for improvement
- **S-R1.** Add explicit **split/merge and reopen re-attribution rules** (resolve DM-Q7/Q8) before schema design — these are the model's biggest gaps and they touch a public metric.
- **S-R2.** Decide **Confirmation revocability** (DM-Q5) and state it as an invariant either way, so the corroboration count's monotonicity is defined.
- **S-R3.** Make the **derived-vs-stored** nature of corroboration count and proximity explicit for the schema doc, with BR-22/C-10 as the governing rules, to prevent drift.
- **S-R4.** Confirm whether **Department** and **anonymous reporting** are in for the prototype; both are currently deferred assumptions that materially shape ownership rules.
- **S-R5.** Name a **retention/archival stance** for append-only histories now (even if "unbounded for prototype"), so it's a conscious decision rather than an oversight.

**Overall:** the model faithfully encodes the PRD's structural decisions — Report/Issue separation, severity-not-score, display-only context, auditability. Its genuine gaps are the **operational rules around merge/split/reopen/revocation**, which are lifecycle edge cases the PRD implies but does not fully specify. Resolving the open questions above closes them without any redesign.

---

## Traceability — Source → Model

| Source concept | Model home |
|----------------|-----------|
| PRD §6.1 core entities | User, Report, Issue, Category, POI, Status/Audit Event, Notification |
| FR-16/17 corroboration & proximity (display-only) | Confirmation, POI (derived), BR-22/24, C-10 |
| FR-18 clustering | Issue, Clustering Rule, BR-13/14, §4.3 |
| FR-14/15/20 severity & override | Severity value, Issue attributes, BR-8/11/12/20/21 |
| FR-2/3 authority scope & RBAC | User↔Category scope, BR-25/26/27, permission table |
| §6.3 lifecycle | Issue Entity Lifecycle |
| FR-27/28/29 notifications | Notification, Notification Preference |
| FR-32 audit | Audit Event, BR-28/31, C-9 |
| FR-30 reference data | Category, POI, Severity Keyword, Clustering Rule, City Boundary |
| FR-7/P3 media & EXIF | Media, BR-4 |
| P6 deletion | BR-33, C-14 |

---

*End of `docs/03-data-model.md` (v1.0). This document defines the business domain only — no schema, no implementation. The next document, `04-api-specification.md`, defines the API contracts over these entities. Resolve the Open Questions (esp. DM-Q7 merge/split re-attribution and DM-Q2 anonymous reporting) before finalizing.*
