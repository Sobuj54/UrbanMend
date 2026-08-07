"""
T1.9 — `/users/me`: profile read/update and account deletion → PII anonymization.

What earns a test here, beyond the happy paths:

- **The privilege fields.** `PATCH {"role":"admin"}` must be *rejected*, not silently dropped. DRF's
  default is to ignore unknown keys, so the wrong implementation answers `200` with an unchanged
  body — indistinguishable from success to the client, and a real escalation the day someone swaps
  `ProfileUpdateSerializer` for a `ModelSerializer`.
- **`email` being unchangeable**, which is a security decision (spec amended 2026-08-07), not an
  omission — so it is asserted rather than assumed.
- **Anonymization actually erasing PII and revoking the session**, checked against the database and
  a follow-up request, not against the `202`.
- **The row surviving** (C-14) — a `delete()` would pass any test that only asserts the PII is gone.
- **Citizen-only deletion**, both roles refused, with the account left untouched afterwards.
- **`categoryScope` present for all three roles** — the shape guarantee the spec amendment makes.

Layered per `testing.md`: service tests own the rules, view tests own the HTTP contract.

[doc: Plan T1.9; API §6.2; PRD P6, BR-30, BR-33, C-14; data-model "Ownership & Permissions"]
"""

from __future__ import annotations

import pytest
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from urbenmend.classification.models import Category
from urbenmend.identity import services
from urbenmend.identity.models import Channel, Role, User, UserStatus, VerificationCode

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-1"
EMAIL = "citizen@example.test"


def _user(**overrides: object) -> User:
    fields: dict[str, object] = {
        "email": EMAIL,
        "password": PASSWORD,
        "status": UserStatus.ACTIVE,
        "role": Role.CITIZEN,
    }
    fields.update(overrides)
    return User.objects.create_user(**fields)  # type: ignore[arg-type]


def _signed_in(user: User) -> Client:
    """A client holding a real session for `user` — `force_login` goes through `django_login()`."""
    client = Client()
    client.force_login(user)
    return client


def _url() -> str:
    return reverse("api:users-me")


# ---------------------------------------------------------------------------------------
# update_profile — the service rules
# ---------------------------------------------------------------------------------------


def test_update_profile_sets_the_phone_and_the_language() -> None:
    user = _user()

    services.update_profile(user=user, phone="+8801712345678", preferred_language="bn")

    user.refresh_from_db()
    assert user.phone == "+8801712345678"
    assert user.preferred_language == "bn"


def test_a_language_only_update_leaves_the_phone_alone() -> None:
    """⚠️ `phone=None` means "not submitted", and must not be read as "clear it"."""
    user = _user(phone="+8801712345678")

    services.update_profile(user=user, preferred_language="bn")

    user.refresh_from_db()
    assert user.phone == "+8801712345678"


def test_a_new_phone_clears_its_verification_and_issues_a_code() -> None:
    """BR-30 — an unverified channel receives no notifications, so it must fail closed."""
    user = _user(phone="+8801712345678")
    services.verify_code(
        user=user,
        channel=Channel.PHONE,
        code=services.send_verification_code(user=user, channel=Channel.PHONE)[1],
    )
    user.refresh_from_db()
    assert user.phone_verified_at is not None

    services.update_profile(user=user, phone="+8801799999999")

    user.refresh_from_db()
    assert user.phone_verified_at is None
    assert VerificationCode.objects.filter(user=user, channel=Channel.PHONE).exists()


def test_resubmitting_the_same_phone_still_clears_verification() -> None:
    """⚠️ Deliberate: the timestamp claims *this* number was proven, and re-verifying is cheap.

    Skipping when the value is unchanged keeps a stale claim alive for a number that changed hands
    and came back.
    """
    user = _user(phone="+8801712345678")
    user.phone_verified_at = user.date_joined
    user.save(update_fields=["phone_verified_at"])

    services.update_profile(user=user, phone="+8801712345678")

    user.refresh_from_db()
    assert user.phone_verified_at is None


def test_an_empty_phone_clears_the_channel() -> None:
    """`""` is an explicit removal — and `save()` normalizes it to the NULL the index needs."""
    user = _user(phone="+8801712345678")

    services.update_profile(user=user, phone="")

    user.refresh_from_db()
    # ⚠️ `None`, never `""` — Postgres allows many NULLs under UNIQUE but only one `""` (A6).
    assert user.phone is None


def test_clearing_the_only_contact_channel_is_rejected() -> None:
    """An account with neither email nor phone is unreachable and unrecoverable (data-model §1)."""
    user = _user(email=None, phone="+8801712345678")

    with pytest.raises(ValidationError):
        services.update_profile(user=user, phone="")

    user.refresh_from_db()
    assert user.phone == "+8801712345678"


def test_a_phone_held_by_another_account_is_a_conflict() -> None:
    _user(email="other@example.test", phone="+8801712345678")
    user = _user()

    with pytest.raises(services.ProfileUpdateError):
        services.update_profile(user=user, phone="+8801712345678")


def test_resubmitting_your_own_number_is_not_a_conflict() -> None:
    """⚠️ Without `.exclude(pk=user.pk)` an unchanged profile would `409` against itself."""
    user = _user(phone="+8801712345678")

    services.update_profile(user=user, phone="+8801712345678")

    user.refresh_from_db()
    assert user.phone == "+8801712345678"


def test_update_profile_cannot_change_the_email() -> None:
    """⚠️ The function has no `email` parameter, and that is the security boundary.

    The email is where a password reset is sent, so a self-service change turns one borrowed
    session into permanent account takeover (API §6.2, amended 2026-08-07).
    """
    import inspect

    assert "email" not in inspect.signature(services.update_profile).parameters


# ---------------------------------------------------------------------------------------
# anonymize_account — P6, BR-33, C-14
# ---------------------------------------------------------------------------------------


def test_anonymization_clears_the_pii_and_flips_the_status() -> None:
    user = _user(phone="+8801712345678")

    services.anonymize_account(user=user)

    user.refresh_from_db()
    assert user.status == UserStatus.DELETED
    assert user.email is None
    assert user.phone is None
    assert user.email_verified_at is None
    assert user.phone_verified_at is None


def test_anonymization_retains_the_row() -> None:
    """⚠️ C-14 — public Issue history keeps a stable author reference. A `delete()` here would
    pass every PII assertion above and destroy the history the constraint exists to protect."""
    user = _user()
    user_id = user.pk

    services.anonymize_account(user=user)

    assert User.objects.filter(pk=user_id).exists()


def test_an_anonymized_account_cannot_authenticate() -> None:
    """`is_active` is derived from `status`, so `DELETED` stops authentication at commit (A6)."""
    user = _user()

    services.anonymize_account(user=user)

    user.refresh_from_db()
    assert user.is_active is False


def test_anonymization_revokes_every_session() -> None:
    """⚠️ BR-33. `is_active` alone does not end a *live* session (Arch §8, T1.3)."""
    user = _user()
    client = _signed_in(user)
    assert Session.objects.count() == 1

    services.anonymize_account(user=user)

    assert Session.objects.count() == 0
    assert SESSION_KEY not in client.session


def test_anonymization_removes_the_secrets_and_the_scope() -> None:
    """⚠️ No cascade fires — the user row survives — so these deletes must be explicit."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    user = _user()
    services.send_verification_code(user=user, channel=Channel.EMAIL)
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)

    services.anonymize_account(user=user)

    assert not VerificationCode.objects.filter(user=user).exists()
    assert not TOTPDevice.objects.filter(user=user).exists()
    assert user.category_scope.count() == 0


def test_an_authority_cannot_self_delete() -> None:
    """API §6.2 amended 2026-08-07 — the ownership matrix grants an Authority `RU`, not `D`."""
    user = _user(email="authority@example.test", role=Role.AUTHORITY)

    with pytest.raises(services.AccountDeletionError):
        services.anonymize_account(user=user)

    user.refresh_from_db()
    assert user.status == UserStatus.ACTIVE
    assert user.email == "authority@example.test"


def test_an_admin_cannot_self_delete() -> None:
    """Self-deletion could strand the platform without an account able to provision anyone."""
    user = _user(email="admin@example.test", role=Role.ADMIN)

    with pytest.raises(services.AccountDeletionError):
        services.anonymize_account(user=user)

    user.refresh_from_db()
    assert user.status == UserStatus.ACTIVE


# ---------------------------------------------------------------------------------------
# GET /users/me — the contract
# ---------------------------------------------------------------------------------------


def test_get_me_returns_the_spec_shape(client: Client) -> None:
    user = _user(phone="+8801712345678")
    client.force_login(user)

    response = client.get(_url())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "email",
        "phone",
        "role",
        "status",
        "preferredLanguage",
        "verified",
        "categoryScope",
        "dateJoined",
    }
    assert body["id"] == str(user.id)
    assert body["role"] == "citizen"
    assert body["status"] == "active"
    assert body["email"] == EMAIL
    assert body["phone"] == "+8801712345678"
    assert body["preferredLanguage"] == "en"
    # Both `false`: `create_user` sets no `*_verified_at`, and API §6.2 derives `verified` from
    # those timestamps rather than from `status` — an `active` account with an unverified channel
    # is a real state (T1.2).
    assert body["verified"] == {"email": False, "phone": False}


def test_get_me_requires_a_session(client: Client) -> None:
    assert client.get(_url()).status_code == 401


def test_get_me_emits_category_scope_for_every_role(client: Client) -> None:
    """⚠️ The T1.9 shape guarantee (spec amended 2026-08-07): `[]` for roles scope does not gate."""
    for role in (Role.CITIZEN, Role.AUTHORITY, Role.ADMIN):
        user = _user(email=f"{role}@example.test", role=role)
        client.force_login(user)

        body = client.get(_url()).json()

        assert body["categoryScope"] == []
        assert body["role"] == role
        client.logout()


def test_get_me_lists_the_authoritys_granted_slugs(client: Client) -> None:
    """⚠️ Ordered by `Category.Meta.ordering` (`name_en`), NOT by the order they were granted.

    "Electrical Hazards" sorts before "Roads & Transport", so this list is the reverse of the
    `add()` call above — deliberately. An unordered M2M read returns rows in whatever order
    Postgres happens to produce, so two `GET /users/me` calls could differ for no reason a client
    could explain; `category_scope_for` exists to pin that down.
    """
    authority = _user(email="roads@example.test", role=Role.AUTHORITY)
    authority.category_scope.add(
        Category.objects.get(slug="roads"),
        Category.objects.get(slug="electrical"),
    )
    client = _signed_in(authority)

    body = client.get(_url()).json()

    assert body["categoryScope"] == ["electrical", "roads"]


# ---------------------------------------------------------------------------------------
# PATCH /users/me — the contract, and the privilege fields
# ---------------------------------------------------------------------------------------


def test_patch_me_updates_and_returns_the_profile(client: Client) -> None:
    user = _user()
    client.force_login(user)

    response = client.patch(
        _url(),
        data={"phone": "+8801712345678", "preferredLanguage": "bn"},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == "+8801712345678"
    assert body["preferredLanguage"] == "bn"
    assert body["verified"]["phone"] is False


def test_patch_me_requires_a_session(client: Client) -> None:
    response = client.patch(
        _url(),
        data={"preferredLanguage": "bn"},
        content_type="application/json",
    )

    assert response.status_code == 401


def test_patch_me_rejects_a_privilege_field(client: Client) -> None:
    """⚠️ The test this file exists for.

    DRF drops unknown keys by default, so the wrong implementation answers `200` with a body still
    reading `role: "citizen"` — and the client cannot tell that apart from a successful update.
    """
    user = _user()
    client.force_login(user)

    response = client.patch(
        _url(),
        data={"role": "admin"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    user.refresh_from_db()
    assert user.role == Role.CITIZEN


def test_patch_me_rejects_a_status_change(client: Client) -> None:
    """`status` is the BR-25 moderation column — self-assignable would undo a suspension."""
    user = _user()
    client.force_login(user)

    response = client.patch(
        _url(),
        data={"status": "active", "preferredLanguage": "bn"},
        content_type="application/json",
    )

    assert response.status_code == 400
    user.refresh_from_db()
    # ⚠️ The legitimate field in the same body must not land either — the request is rejected whole.
    assert user.preferred_language == "en"


def test_patch_me_rejects_an_email_change(client: Client) -> None:
    """Excluded by the spec amendment: the email is where a password reset is sent."""
    user = _user()
    client.force_login(user)

    response = client.patch(
        _url(),
        data={"email": "attacker@example.test"},
        content_type="application/json",
    )

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.email == EMAIL


def test_patch_me_rejects_an_empty_body(client: Client) -> None:
    """⚠️ A `200` here is indistinguishable from a real update, so a misspelled field reads OK."""
    user = _user()
    client.force_login(user)

    response = client.patch(_url(), data={}, content_type="application/json")

    assert response.status_code == 400


def test_patch_me_rejects_a_null_phone(client: Client) -> None:
    """`""` clears the number; `null` is refused rather than aliased to it."""
    user = _user(phone="+8801712345678")
    client.force_login(user)

    response = client.patch(
        _url(),
        data={"phone": None},
        content_type="application/json",
    )

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.phone == "+8801712345678"


def test_patch_me_returns_409_on_a_taken_phone(client: Client) -> None:
    """API §6.2 lists `409` (identity in use) for this endpoint."""
    _user(email="other@example.test", phone="+8801712345678")
    client.force_login(_user())

    response = client.patch(
        _url(),
        data={"phone": "+8801712345678"},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_patch_me_returns_422_when_clearing_the_last_channel(client: Client) -> None:
    """A business-rule rejection, so `422` — not the `400` a malformed body gets."""
    client.force_login(_user(email=None, phone="+8801712345678"))

    response = client.patch(_url(), data={"phone": ""}, content_type="application/json")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_patch_me_rejects_an_unknown_language(client: Client) -> None:
    client.force_login(_user())

    response = client.patch(
        _url(),
        data={"preferredLanguage": "fr"},
        content_type="application/json",
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------------------
# DELETE /users/me — the contract
# ---------------------------------------------------------------------------------------


def test_delete_me_returns_202_with_no_body(client: Client) -> None:
    """API §6.2 `202`. No body — echoing the profile would return the PII just erased."""
    user = _user()
    client.force_login(user)

    response = client.delete(_url())

    assert response.status_code == 202
    assert not response.content


def test_delete_me_anonymizes_and_ends_the_session(client: Client) -> None:
    """⚠️ The `202` goes back to a credential that no longer authenticates."""
    user = _user()
    client.force_login(user)

    client.delete(_url())

    user.refresh_from_db()
    assert user.status == UserStatus.DELETED
    assert user.email is None
    # The same client, immediately afterwards: the session is already gone.
    assert client.get(_url()).status_code == 401


def test_delete_me_requires_a_session(client: Client) -> None:
    assert client.delete(_url()).status_code == 401


def test_delete_me_is_forbidden_for_an_authority(client: Client) -> None:
    """Spec amended 2026-08-07 — self-service deletion is Citizen-only."""
    authority = _user(email="authority@example.test", role=Role.AUTHORITY)
    client.force_login(authority)

    response = client.delete(_url())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    authority.refresh_from_db()
    assert authority.status == UserStatus.ACTIVE
    assert authority.email == "authority@example.test"


def test_delete_me_is_forbidden_for_an_admin(client: Client) -> None:
    admin = _user(email="admin@example.test", role=Role.ADMIN)
    client.force_login(admin)

    assert client.delete(_url()).status_code == 403


# ---------------------------------------------------------------------------------------
# Rate-limit headers — api-conventions.md: "All protected endpoints implicitly return 401 and 429"
# ---------------------------------------------------------------------------------------


def test_me_advertises_its_rate_limit(client: Client) -> None:
    client.force_login(_user())

    response = client.get(_url())

    assert response["RateLimit-Limit"] == "20"  # `auth_user`, the default from settings
    assert int(response["RateLimit-Remaining"]) >= 0
