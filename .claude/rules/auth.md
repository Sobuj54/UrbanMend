---
description: Sessions, CSRF, roles, and service-layer authorization
paths:
  - "identity/**"
  - "**/permissions.py"
  - "**/middleware.py"
  - "**/authentication.py"
---

# Auth & authorization

Source: `docs/02-architecture.md` §8, `docs/04-api-specification.md` §2, PRD §4.2.

## Sessions, not JWT

Approved mechanism is **server-validated sessions**, chosen over stateless JWT for reliable
**immediate revocation** (moderation, deprovisioning). No cross-service token sharing is needed at
this scale.

- Django's session framework on the **`cached_db`** backend.
- Login issues an **opaque session token** in a **`Secure`, `HttpOnly`, `SameSite`** cookie. Not
  readable by JavaScript.
- CSRF token required on state-changing requests (double-submit cookie or header token); `GET`/`HEAD`
  exempt. Carried by DRF `SessionAuthentication`.
- Revocation is immediate server-side — deleting `django_session` rows.
- **No refresh tokens.** The concept does not exist here.

Any divergence in cookie flags, exemptions, or the CSRF header name is a **defect in the
implementation**, not an amendment to the spec.

## Roles

One `role` field on the user entity: **Citizen**, **Authority**, **Admin**.

- Every protected endpoint declares its required role plus conditions: own-resource, authority
  category scope (BR-26), pre-triage editability, mandatory reason.
- **BR-26** — an Authority may view/act on Issues **only within their category scope**.
- **BR-25** — the Authority role can be granted **only by an Admin**, and the grant is audited.
- **BR-15** — no one may advance an Issue past `Triaged` without the Authority role.
- Authority accounts are admin-provisioned, not self-serve (FR-2).

**Do not use `django.contrib.auth` Groups/Permissions for this.** They cannot express the
category/department scoping BR-26 requires. RBAC is an explicit `role` field plus an
authority↔category scope relation, evaluated in the service layer.

## Enforcement point

**FR-3: authorization is enforced in the service layer** (`services.py`), on **every** server-side
action — not just in the UI, and not primarily in views. DRF permission classes are
**defence-in-depth, not the enforcement point**. Authorization logic must not scatter into views and
serializers (R-12/DC-3).

Scope leakage returns `403`/`404` deliberately, to avoid existence leaks. The API never returns
another user's contact info.

## Access rules

- **Login required for all submissions.** Anonymous write access is not supported (Q4 resolved).
- **Public reads:** the map and issue list are visible to unauthenticated users (Q7 resolved).
- Unverified accounts cannot receive notifications on an unverified channel (BR-30) and have limited
  capability — the exact limited set is **not specified**; don't invent it.

## Credentials & 2FA

- Password hashing: **Argon2** (`argon2-cffi`), T1.2.
- 2FA via `django-otp` — Authority/Admin may be required to use it (FR-4, optional per policy).
  `POST /auth/2fa/verify` runs on a partial post-password session.
- Password reset takes a `resetToken` in the body. Reset/registration responses are generic.
- `DJANGO_SECRET_KEY` must be **stable across deployments** — rotating it invalidates signed cookies,
  session hashes, and signed export URLs. Provisioned once per environment, never per build.
- Never log or echo secret values.

## Build order

Auth/User is foundational — build it first, before dependent features. The **custom user model must
be declared before the first migration** (irreversible afterwards, T0.10).
