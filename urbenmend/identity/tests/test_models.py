"""
Tests for the custom user model [doc: Plan T0.10/T1.1, data-model §1].

These cover the invariants that are cheap to break silently later: the DB-level contact
constraint and its anonymization escape hatch, `is_active` being derived from `status`, contact
normalization on both the clean() and save() paths, and the manager's create entry points.

⚠️ `django_db` is required on nearly every case — the CheckConstraint and the UNIQUE indexes
are database behaviour, and asserting them against unsaved instances would pass while the real
schema was wrong.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from urbenmend.identity.models import Language, Role, User, UserStatus


def test_auth_user_model_is_identity_user() -> None:
    """The A6 deliverable itself: AUTH_USER_MODEL resolves to this class.

    If this fails, a migration was likely generated against auth.User and the database must be
    dropped rather than repaired [doc: Arch §2.4].
    """
    assert get_user_model() is User


class TestContactConstraint:
    """ "email and/or phone", enforced in the database, not just in Python (data-model §1)."""

    @pytest.mark.django_db
    def test_email_only_is_allowed(self) -> None:
        user = User.objects.create_user(email="citizen@example.com", password="pw-for-test-only")
        assert user.pk is not None
        assert user.phone is None

    @pytest.mark.django_db
    def test_phone_only_is_allowed(self) -> None:
        """OTP-only signup (FR-1) — no email, and therefore USERNAME_FIELD is None."""
        user = User.objects.create_user(phone="+8801712345678")
        assert user.pk is not None
        assert user.email is None
        assert not user.has_usable_password()

    @pytest.mark.django_db
    def test_neither_is_rejected_by_the_manager(self) -> None:
        with pytest.raises(ValueError, match="email address or a phone number"):
            User.objects.create_user()

    @pytest.mark.django_db
    def test_neither_is_rejected_by_the_database(self) -> None:
        """Bypassing the manager must still fail — the constraint is the real guarantee."""
        with pytest.raises(IntegrityError, match="identity_user_has_contact_or_anonymized"):
            User(email=None, phone=None).save()

    @pytest.mark.django_db
    def test_anonymized_user_may_have_no_contact(self) -> None:
        """The DELETED escape hatch. Without it, DELETE /users/me could not anonymize while
        retaining the row for referential integrity (P6, BR-33, C-14).
        """
        user = User.objects.create_user(email="leaving@example.com")
        user.email = None
        user.phone = None
        user.status = UserStatus.DELETED
        user.save()

        user.refresh_from_db()
        assert user.email is None
        assert user.status == UserStatus.DELETED


class TestUniqueness:
    @pytest.mark.django_db
    def test_duplicate_email_is_rejected(self) -> None:
        User.objects.create_user(email="dup@example.com")
        with pytest.raises(IntegrityError):
            User.objects.create_user(email="dup@example.com")

    @pytest.mark.django_db
    def test_duplicate_email_differing_only_in_case_is_rejected(self) -> None:
        """Normalization must happen before the UNIQUE check, or one mailbox becomes two
        accounts.
        """
        User.objects.create_user(email="Mixed@Example.com")
        with pytest.raises(IntegrityError):
            User.objects.create_user(email="mixed@example.com")

    @pytest.mark.django_db
    def test_many_users_may_have_no_phone(self) -> None:
        """The reason absence is NULL and not "": Postgres permits many NULLs under a UNIQUE
        index but only one empty string.
        """
        User.objects.create_user(email="a@example.com")
        User.objects.create_user(email="b@example.com")
        assert User.objects.filter(phone__isnull=True).count() == 2


class TestIsActive:
    """Derived from `status`, so the two can never contradict each other."""

    @pytest.mark.parametrize(
        "status",
        [UserStatus.REGISTERED, UserStatus.VERIFIED, UserStatus.ACTIVE],
    )
    def test_pre_suspension_statuses_may_authenticate(self, status: UserStatus) -> None:
        assert User(email="x@example.com", status=status).is_active is True

    @pytest.mark.parametrize(
        "status",
        [UserStatus.SUSPENDED, UserStatus.DEPROVISIONED, UserStatus.DELETED],
    )
    def test_terminated_statuses_may_not_authenticate(self, status: UserStatus) -> None:
        assert User(email="x@example.com", status=status).is_active is False

    def test_is_active_cannot_be_assigned(self) -> None:
        """Read-only on purpose — callers must change `status` (T1.9), so no code path can set
        a value that `status` silently contradicts.
        """
        user = User(email="x@example.com", status=UserStatus.ACTIVE)
        with pytest.raises(AttributeError):
            user.is_active = False  # type: ignore[misc]

    def test_every_status_is_classified(self) -> None:
        """Guards the enum against growing a member that `is_active` never considers."""
        active = {UserStatus.REGISTERED, UserStatus.VERIFIED, UserStatus.ACTIVE}
        inactive = {UserStatus.SUSPENDED, UserStatus.DEPROVISIONED, UserStatus.DELETED}
        assert active | inactive == set(UserStatus)


class TestNormalization:
    @pytest.mark.django_db
    def test_save_lowercases_and_strips_email(self) -> None:
        user = User.objects.create_user(email="  Spaced@Example.COM  ")
        assert user.email == "spaced@example.com"

    @pytest.mark.django_db
    def test_save_converts_blank_contact_to_null(self) -> None:
        """Normalizing in save() and not only clean() matters because DRF serializers never
        call full_clean() — the API path would otherwise store "".
        """
        user = User(email="kept@example.com", phone="")
        user.save()
        user.refresh_from_db()
        assert user.phone is None

    def test_clean_normalizes_without_a_username_field(self) -> None:
        """AbstractBaseUser.clean() would raise TypeError on a phone-only account; ours must
        not call super() for exactly that reason.
        """
        user = User(email=None, phone="  +8801712345678  ")
        user.clean()
        assert user.phone == "+8801712345678"
        assert user.email is None


class TestManager:
    @pytest.mark.django_db
    def test_create_user_defaults_to_an_unprivileged_citizen(self) -> None:
        user = User.objects.create_user(email="new@example.com", password="pw-for-test-only")
        assert user.role == Role.CITIZEN
        assert user.status == UserStatus.REGISTERED
        assert user.preferred_language == Language.ENGLISH
        assert user.is_staff is False
        assert user.is_superuser is False
        assert user.email_verified_at is None

    @pytest.mark.django_db
    def test_create_user_hashes_the_password(self) -> None:
        user = User.objects.create_user(email="pw@example.com", password="pw-for-test-only")
        assert user.password != "pw-for-test-only"
        assert user.check_password("pw-for-test-only") is True

    @pytest.mark.django_db
    def test_create_superuser_is_an_active_admin(self) -> None:
        user = User.objects.create_superuser(email="admin@example.com", password="pw-for-test-only")
        assert user.role == Role.ADMIN
        assert user.status == UserStatus.ACTIVE
        assert user.is_staff is True
        assert user.is_superuser is True
        assert user.is_active is True

    @pytest.mark.django_db
    def test_create_superuser_requires_email_and_password(self) -> None:
        with pytest.raises(ValueError, match="requires an email address"):
            User.objects.create_superuser(password="pw-for-test-only")
        with pytest.raises(ValueError, match="requires a password"):
            User.objects.create_superuser(email="admin@example.com")


class TestIdentifiers:
    @pytest.mark.django_db
    def test_pk_is_a_non_sequential_uuid(self) -> None:
        """API §1.2 forbids guessable IDs in URLs; sequential integers would leak the user
        count and allow enumeration.
        """
        first = User.objects.create_user(email="one@example.com")
        second = User.objects.create_user(email="two@example.com")
        assert isinstance(first.pk, uuid.UUID)
        assert first.pk != second.pk

    @pytest.mark.django_db
    def test_str_prefers_email_then_phone_then_pk(self) -> None:
        assert str(User(email="e@example.com", phone="+8801712345678")) == "e@example.com"
        assert str(User(phone="+8801712345678")) == "+8801712345678"
        anonymized = User(status=UserStatus.DELETED)
        assert str(anonymized) == str(anonymized.pk)


class TestRbacBoundary:
    def test_role_values_match_the_wire_format(self) -> None:
        """Stored value and API value are deliberately identical (API §6.2) so no mapping
        layer can drift between them.
        """
        assert {r.value for r in Role} == {"citizen", "authority", "admin"}

    @pytest.mark.django_db
    def test_domain_role_is_independent_of_contrib_auth_groups(self) -> None:
        """⚠️ Groups exist only as admin plumbing. Domain RBAC is `role` + category scope in
        the service layer, because Groups cannot express BR-26 per-category scoping.
        """
        user = User.objects.create_user(email="authority@example.com", role=Role.AUTHORITY)
        assert user.role == Role.AUTHORITY
        assert user.groups.count() == 0
        assert user.user_permissions.count() == 0
