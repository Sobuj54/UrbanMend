"""
Authority provisioning (T1.6) — FR-2, BR-25, BR-26, API §6.2.

What these cover, and why each earns its place:

- **BR-25 "only by an Admin"**, from every other caller: Citizen, Authority (an Authority must not
  be able to clone itself), unauthenticated, and a *suspended* Admin whose `role` column still
  reads `admin`.
- **The scope actually landing.** A `201` that returns `categoryScope` from the request body
  instead of from the database would pass any test that only reads the response.
- **The provisioned account's starting state** — `registered`, unusable password, no session. Each
  is a deliberate decision in `provision_authority` and each is invisible from the wire.
- **Retired categories rejected**, which is the failure mode that otherwise ships silently: the
  grant succeeds and the authority can act on nothing.
- **`409` on a duplicate, specific message.** The opposite disclosure rule to registration, so the
  two are asserted to differ on purpose rather than by accident.

⚠️ `django_db` throughout — scope is an M2M and has no meaning on an unsaved instance.

[doc: Plan T1.6; PRD FR-2, FR-4; data-model BR-25/26; API §6.2; auth.md]
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from urbenmend.audit.models import AuditEvent
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.identity.models import Role, User, UserStatus
from urbenmend.identity.services import (
    AuthorizationError,
    ProvisioningError,
    provision_authority,
    set_category_scope,
)

pytestmark = pytest.mark.django_db

PASSWORD = "provisioning-password-for-test-only"


def _user(*, role: str, email: str, status_: str = UserStatus.ACTIVE) -> User:
    return User.objects.create_user(
        email=email,
        password=PASSWORD,
        role=role,
        status=status_,
    )


@pytest.fixture
def admin() -> User:
    return _user(role=Role.ADMIN, email="admin@example.test")


@pytest.fixture
def citizen() -> User:
    return _user(role=Role.CITIZEN, email="citizen@example.test")


@pytest.fixture
def authority() -> User:
    return _user(role=Role.AUTHORITY, email="authority@example.test")


class TestProvisionAuthorityAuthorization:
    """BR-25 — "the Authority role can be granted only by an Admin"."""

    def test_admin_can_provision(self, admin: User) -> None:
        authority = provision_authority(
            actor=admin,
            email="roads.officer@example.test",
            category_slugs=["roads"],
        )

        assert authority.role == Role.AUTHORITY
        assert authority.pk != admin.pk
        event = AuditEvent.objects.get(action="authority.provisioned")
        assert event.actor == admin
        assert event.target == authority
        assert event.after == {"category_scope": ["roads"], "require_two_factor": False}

    def test_citizen_cannot_provision(self, citizen: User) -> None:
        with pytest.raises(AuthorizationError):
            provision_authority(
                actor=citizen,
                email="self.promoted@example.test",
                category_slugs=["roads"],
            )

        assert not User.objects.filter(email="self.promoted@example.test").exists()

    def test_authority_cannot_provision_another_authority(self, authority: User) -> None:
        """⚠️ The privilege-escalation case BR-25 is written against. An Authority holds real
        power in the system, so "an admin-only action" that any Authority could perform would let
        one scoped officer mint an unscoped colleague and route around BR-26 entirely."""
        with pytest.raises(AuthorizationError):
            provision_authority(
                actor=authority,
                email="colleague@example.test",
                category_slugs=["electrical"],
            )

        assert not User.objects.filter(email="colleague@example.test").exists()

    def test_suspended_admin_cannot_provision(self) -> None:
        """`role` still reads `admin`; only `status` moved. `has_role()` consults `is_active`,
        which is derived from `status` — the same reasoning as T1.5's suspended Authority."""
        suspended = _user(
            role=Role.ADMIN,
            email="suspended.admin@example.test",
            status_=UserStatus.SUSPENDED,
        )

        assert suspended.role == Role.ADMIN
        with pytest.raises(AuthorizationError):
            provision_authority(
                actor=suspended,
                email="ghost@example.test",
                category_slugs=["roads"],
            )

    def test_anonymous_actor_is_denied(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        anonymous: User = AnonymousUser()  # type: ignore[assignment]

        with pytest.raises(AuthorizationError):
            provision_authority(
                actor=anonymous,
                email="anon.grant@example.test",
                category_slugs=["roads"],
            )

    def test_authorization_precedes_validation(self, citizen: User) -> None:
        """⚠️ A non-Admin sending a malformed body must still get `403`, not `422`. If validation
        ran first, the error message would tell an unauthorized caller which category keys are
        valid — a small leak, but the ordering is free and the reverse is not."""
        with pytest.raises(AuthorizationError):
            provision_authority(actor=citizen, email=None, phone=None, category_slugs=["nope"])


class TestProvisionedAccountState:
    """The starting state of the new account — every field a deliberate choice."""

    def test_scope_is_persisted_not_echoed(self, admin: User) -> None:
        """⚠️ Re-read from the database, not from the returned instance's cached M2M. A `.set()`
        that silently failed would leave the in-memory object looking correct."""
        authority = provision_authority(
            actor=admin,
            email="roads.officer@example.test",
            category_slugs=["roads", "water_drainage"],
        )

        stored = User.objects.get(pk=authority.pk)
        assert set(stored.category_scope.values_list("slug", flat=True)) == {
            "roads",
            "water_drainage",
        }

    def test_status_is_registered_not_active(self, admin: User) -> None:
        """The work address is unproven until someone reading that mailbox verifies it (BR-30).
        An Admin typo would otherwise create a live Authority whose owner never learns of it."""
        authority = provision_authority(
            actor=admin,
            email="unverified@example.test",
            category_slugs=["roads"],
        )

        assert authority.status == UserStatus.REGISTERED
        assert authority.email_verified_at is None

    def test_password_is_unusable(self, admin: User) -> None:
        """⚠️ No password is set, and none is generated. An Admin choosing another person's
        credential, or a generated secret travelling back in the API response, are both worse than
        an account that cannot yet authenticate. T1.7's reset flow is the intended path."""
        authority = provision_authority(
            actor=admin,
            email="nopassword@example.test",
            category_slugs=["roads"],
        )

        assert not authority.has_usable_password()
        assert not authority.check_password(PASSWORD)

    def test_require_two_factor_is_stored(self, admin: User) -> None:
        """API §6.2 sends `requireTwoFactor`. Discarding a documented input would tell the Admin
        the account requires 2FA while nothing recorded that it does. Enforcement is T1.7."""
        authority = provision_authority(
            actor=admin,
            email="twofactor@example.test",
            category_slugs=["roads"],
            require_two_factor=True,
        )

        assert User.objects.get(pk=authority.pk).require_two_factor is True

    def test_require_two_factor_defaults_off(self, admin: User) -> None:
        authority = provision_authority(
            actor=admin,
            email="notwofactor@example.test",
            category_slugs=["roads"],
        )

        assert authority.require_two_factor is False

    def test_email_is_normalized_before_storage(self, admin: User) -> None:
        authority = provision_authority(
            actor=admin,
            email="  Mixed.Case@Example.Test  ",
            category_slugs=["roads"],
        )

        assert authority.email == "mixed.case@example.test"

    def test_phone_only_authority_is_allowed(self, admin: User) -> None:
        """Email *and/or* phone (data-model §1) — the endpoint's example body shows email, but the
        user entity accepts either, and an authority reachable only by handset is legitimate."""
        authority = provision_authority(
            actor=admin,
            phone="+8801712345678",
            category_slugs=["roads"],
        )

        assert authority.email is None
        assert authority.phone == "+8801712345678"


class TestProvisioningScope:
    """BR-26 — the scope is the grant."""

    def test_empty_scope_is_allowed_and_grants_nothing(self, admin: User) -> None:
        """A provisioned account awaiting its scope. Rejecting it would invent a requirement
        API §6.2 does not state; `has_category_scope` is what makes it harmless."""
        from urbenmend.identity.services import has_category_scope

        authority = provision_authority(
            actor=admin,
            email="unscoped@example.test",
            category_slugs=[],
        )

        assert authority.category_scope.count() == 0
        assert not has_category_scope(authority, Category.objects.get(slug="roads"))

    def test_unknown_slug_is_rejected_and_nothing_is_written(self, admin: User) -> None:
        with pytest.raises(ValidationError):
            provision_authority(
                actor=admin,
                email="badscope@example.test",
                category_slugs=["roads", "not_a_category"],
            )

        assert not User.objects.filter(email="badscope@example.test").exists()

    def test_retired_category_is_rejected(self, admin: User) -> None:
        """⚠️ The silent-failure case. A Retired node can never match an Issue, so scoping to one
        grants nothing while reading back as a successful grant — the account looks correctly
        configured and does nothing. `422` with the key is the honest answer."""
        retired = Category.objects.get(slug="street_lighting")
        retired.status = CategoryStatus.RETIRED
        retired.save()

        with pytest.raises(ValidationError) as raised:
            provision_authority(
                actor=admin,
                email="retiredscope@example.test",
                category_slugs=["street_lighting"],
            )

        assert "street_lighting" in str(raised.value)

    def test_duplicate_slugs_collapse_to_one_grant(self, admin: User) -> None:
        """`["roads","roads"]` is one grant, not a length mismatch reported as an unknown key."""
        authority = provision_authority(
            actor=admin,
            email="dupescope@example.test",
            category_slugs=["roads", "roads"],
        )

        assert authority.category_scope.count() == 1

    def test_missing_contact_is_rejected(self, admin: User) -> None:
        with pytest.raises(ValidationError):
            provision_authority(actor=admin, category_slugs=["roads"])


class TestProvisioningConflicts:
    """API §6.2 lists `409` for this endpoint."""

    def test_duplicate_email_raises_provisioning_error(self, admin: User, citizen: User) -> None:
        with pytest.raises(ProvisioningError):
            provision_authority(
                actor=admin,
                email=citizen.email,
                category_slugs=["roads"],
            )

    def test_duplicate_email_is_caught_case_insensitively(self, admin: User) -> None:
        """⚠️ The reason the check normalizes with `.lower()` rather than
        `BaseUserManager.normalize_email`, which lowercases only the domain: `Admin@x.com` would
        pass an unnormalized check and then be stored lowercased, surfacing the collision as a
        `500 IntegrityError` instead of the documented `409`."""
        with pytest.raises(ProvisioningError):
            provision_authority(
                actor=admin,
                email="ADMIN@EXAMPLE.TEST",
                category_slugs=["roads"],
            )

    def test_duplicate_phone_raises_provisioning_error(self, admin: User) -> None:
        provision_authority(
            actor=admin,
            phone="+8801712345678",
            category_slugs=["roads"],
        )

        with pytest.raises(ProvisioningError):
            provision_authority(
                actor=admin,
                phone="+8801712345678",
                category_slugs=["electrical"],
            )

    def test_conflict_message_is_specific_unlike_registration(
        self, admin: User, citizen: User
    ) -> None:
        """⚠️ The deliberate asymmetry with `register_citizen`. Registration is public, so its
        `409` must not confirm an address is taken. This endpoint is Admin-only, and an Admin who
        cannot be told "that address already has an account" cannot do the job."""
        with pytest.raises(ProvisioningError) as raised:
            provision_authority(actor=admin, email=citizen.email, category_slugs=["roads"])

        assert "email" in str(raised.value).lower()


class TestSetCategoryScope:
    """BR-25/BR-26 scope changes after provisioning (the service behind API §6.2 `PATCH`)."""

    def test_admin_replaces_the_scope(self, admin: User, authority: User) -> None:
        authority.category_scope.add(Category.objects.get(slug="roads"))

        set_category_scope(actor=admin, authority=authority, category_slugs=["electrical"])

        assert set(authority.category_scope.values_list("slug", flat=True)) == {"electrical"}

    def test_scope_is_replaced_not_merged(self, admin: User, authority: User) -> None:
        """⚠️ The spec sends the whole array, so a merge would make revocation impossible through
        the documented body — an Admin narrowing a scope would silently widen it."""
        authority.category_scope.set(Category.objects.filter(slug__in=["roads", "electrical"]))

        set_category_scope(actor=admin, authority=authority, category_slugs=["roads"])

        assert set(authority.category_scope.values_list("slug", flat=True)) == {"roads"}

    def test_empty_array_revokes_all_scope(self, admin: User, authority: User) -> None:
        """The intended way to park an account without suspending it."""
        authority.category_scope.add(Category.objects.get(slug="roads"))

        set_category_scope(actor=admin, authority=authority, category_slugs=[])

        assert authority.category_scope.count() == 0

    def test_non_admin_cannot_change_scope(self, citizen: User, authority: User) -> None:
        authority.category_scope.add(Category.objects.get(slug="roads"))

        with pytest.raises(AuthorizationError):
            set_category_scope(actor=citizen, authority=authority, category_slugs=["electrical"])

        assert set(authority.category_scope.values_list("slug", flat=True)) == {"roads"}

    def test_scope_on_a_citizen_is_rejected(self, admin: User, citizen: User) -> None:
        with pytest.raises(ValidationError):
            set_category_scope(actor=admin, authority=citizen, category_slugs=["roads"])

    def test_suspended_authority_scope_remains_editable(self, admin: User) -> None:
        """⚠️ The target is checked against the `role` column, not `has_role()`. An Admin must be
        able to correct a scope before reinstating a suspended officer; requiring the account to be
        active first would force reinstating them with the wrong scope."""
        suspended = _user(
            role=Role.AUTHORITY,
            email="suspended.authority@example.test",
            status_=UserStatus.SUSPENDED,
        )

        set_category_scope(actor=admin, authority=suspended, category_slugs=["roads"])

        assert set(suspended.category_scope.values_list("slug", flat=True)) == {"roads"}


class TestProvisionAuthorityEndpoint:
    """`POST /users/authorities` — the wire contract (API §6.2)."""

    @pytest.fixture
    def url(self) -> str:
        return reverse("api:users-authorities")

    def _client(self, user: User | None = None) -> APIClient:
        client = APIClient()
        if user is not None:
            client.force_authenticate(user=user)
        return client

    def test_admin_gets_201_with_the_authority_summary(self, admin: User, url: str) -> None:
        response = self._client(admin).post(
            url,
            {
                "email": "roads.officer@example.test",
                "categoryScope": ["roads"],
                "requireTwoFactor": True,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["role"] == "authority"
        assert response.data["categoryScope"] == ["roads"]
        assert response.data["status"] == "registered"

    def test_response_body_is_camel_case(self, admin: User, url: str) -> None:
        """⚠️ The T0.6 divergence the docs call "the single easiest way for the implementation to
        silently drift" — DRF emits `snake_case` and the contract is `camelCase`."""
        response = self._client(admin).post(
            url,
            {"email": "camel@example.test", "categoryScope": []},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert "categoryScope" in response.data
        assert "category_scope" not in response.data
        assert "preferredLanguage" in response.data

    def test_citizen_gets_403(self, citizen: User, url: str) -> None:
        response = self._client(citizen).post(
            url,
            {"email": "denied@example.test", "categoryScope": ["roads"]},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["error"]["code"] == "FORBIDDEN"

    def test_unauthenticated_gets_401(self, url: str) -> None:
        """⚠️ `401`, not the `403` DRF rewrites unauthenticated session replies to — undone
        globally in `urbenmend_exception_handler` (T1.3). Asserted here because a new view is
        exactly where that fix would be quietly bypassed."""
        response = self._client().post(
            url,
            {"email": "anon@example.test", "categoryScope": ["roads"]},
            format="json",
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.data["error"]["code"] == "UNAUTHENTICATED"

    def test_duplicate_email_gets_409(self, admin: User, citizen: User, url: str) -> None:
        response = self._client(admin).post(
            url,
            {"email": citizen.email, "categoryScope": ["roads"]},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["error"]["code"] == "CONFLICT"

    def test_unknown_category_gets_422(self, admin: User, url: str) -> None:
        response = self._client(admin).post(
            url,
            {"email": "badscope@example.test", "categoryScope": ["not_a_category"]},
            format="json",
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_missing_contact_gets_400_validation_failed(self, admin: User, url: str) -> None:
        response = self._client(admin).post(url, {"categoryScope": ["roads"]}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"]["code"] == "VALIDATION_FAILED"

    def test_role_in_the_body_cannot_escalate(self, admin: User, url: str) -> None:
        """⚠️ The serializer has no `role` field, so this key is ignored rather than honoured. An
        Admin minting another Admin through an endpoint documented to create Authorities would be
        an undocumented privilege path, and `POST /users/authorities` says what it makes."""
        response = self._client(admin).post(
            url,
            {"email": "wannabe.admin@example.test", "role": "admin", "categoryScope": ["roads"]},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["role"] == "authority"
        assert User.objects.get(email="wannabe.admin@example.test").role == Role.AUTHORITY

    def test_status_in_the_body_cannot_activate(self, admin: User, url: str) -> None:
        """Same reasoning as `role`: a caller-supplied `active` would skip channel verification."""
        response = self._client(admin).post(
            url,
            {"email": "wannabe.active@example.test", "status": "active", "categoryScope": []},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["status"] == "registered"

    def test_response_never_carries_a_password_or_hash(self, admin: User, url: str) -> None:
        """The account has an unusable password; leaking the hash string would still be a leak,
        and `UserSerializer` must never grow a credential field."""
        response = self._client(admin).post(
            url,
            {"email": "nosecrets@example.test", "categoryScope": ["roads"]},
            format="json",
        )

        body = str(response.data)
        assert "password" not in body.lower()
        assert "argon2" not in body.lower()
