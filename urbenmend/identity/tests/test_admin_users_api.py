from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.audit.models import AuditEvent
from urbenmend.identity.models import Role, UserStatus
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory

pytestmark = pytest.mark.django_db


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def test_user_list_is_admin_only_and_filterable() -> None:
    admin = AdminFactory()
    authority = AuthorityFactory(email="filter-authority@example.test")
    UserFactory(email="filter-citizen@example.test")
    url = reverse("api:users")
    assert client().get(url).status_code == 401
    assert client(AuthorityFactory()).get(url).status_code == 403
    response = client(admin).get(url, {"role": "authority", "q": "filter-authority", "limit": 1})
    assert response.status_code == 200
    assert response.data["meta"]["count"] == 1
    assert response.data["data"][0]["id"] == str(authority.pk)


def test_admin_updates_status_scope_and_two_factor_with_audit() -> None:
    admin = AdminFactory()
    authority = AuthorityFactory()
    url = reverse("api:users-detail", kwargs={"user_id": authority.pk})
    response = client(admin).patch(
        url,
        {"status": "suspended", "categoryScope": ["roads"], "requireTwoFactor": True},
        format="json",
    )
    assert response.status_code == 200
    authority.refresh_from_db()
    assert authority.status == UserStatus.SUSPENDED
    assert authority.require_two_factor is True
    assert list(authority.category_scope.values_list("slug", flat=True)) == ["roads"]
    event = AuditEvent.objects.get(action="identity.user_updated")
    assert event.target == authority
    assert (
        event.before["status"] == UserStatus.REGISTERED
        or event.before["status"] == UserStatus.ACTIVE
    )
    assert event.after["status"] == UserStatus.SUSPENDED


def test_non_admin_and_invalid_scope_are_rejected_without_mutation() -> None:
    authority = AuthorityFactory()
    url = reverse("api:users-detail", kwargs={"user_id": authority.pk})
    assert (
        client(AuthorityFactory()).patch(url, {"status": "suspended"}, format="json").status_code
        == 403
    )
    response = client(AdminFactory()).patch(
        url, {"categoryScope": ["does-not-exist"]}, format="json"
    )
    assert response.status_code == 422
    authority.refresh_from_db()
    assert authority.status == UserStatus.REGISTERED or authority.status == UserStatus.ACTIVE


def test_role_change_to_citizen_cannot_carry_category_scope() -> None:
    admin = AdminFactory()
    authority = AuthorityFactory()
    response = client(admin).patch(
        reverse("api:users-detail", kwargs={"user_id": authority.pk}),
        {"role": Role.CITIZEN, "categoryScope": ["roads"]},
        format="json",
    )
    assert response.status_code == 400
