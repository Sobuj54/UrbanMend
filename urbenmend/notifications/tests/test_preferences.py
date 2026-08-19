from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.identity.tests.factories import UserFactory
from urbenmend.notifications.models import NotificationPreference

pytestmark = pytest.mark.django_db


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def test_preferences_require_authentication_and_default_enabled_without_writing() -> None:
    url = reverse("api:notification-preferences")
    assert _client().get(url).status_code == 401
    user = UserFactory()
    response = _client(user).get(url)
    assert response.status_code == 200
    assert response.data == {"inApp": True, "email": True}
    assert NotificationPreference.objects.count() == 0


def test_patch_persists_partial_update_and_get_returns_it() -> None:
    user = UserFactory()
    url = reverse("api:notification-preferences")
    response = _client(user).patch(url, {"email": False}, format="json")
    assert response.status_code == 200
    assert response.data == {"inApp": True, "email": False}
    preference = NotificationPreference.objects.get(user=user)
    assert preference.email is False
    assert _client(user).get(url).data == response.data


def test_preferences_are_strictly_self_owned() -> None:
    first = UserFactory()
    second = UserFactory()
    NotificationPreference.objects.create(user=second, in_app=False, email=False)
    response = _client(first).patch(
        reverse("api:notification-preferences"), {"email": False}, format="json"
    )
    assert response.status_code == 200
    second_pref = NotificationPreference.objects.get(user=second)
    assert (second_pref.in_app, second_pref.email) == (False, False)


def test_empty_unknown_and_non_boolean_updates_are_rejected() -> None:
    user = UserFactory()
    url = reverse("api:notification-preferences")
    assert _client(user).patch(url, {}, format="json").status_code == 400
    assert _client(user).patch(url, {"push": True}, format="json").status_code == 400
    assert _client(user).patch(url, {"sms": False}, format="json").status_code == 400
    assert NotificationPreference.objects.count() == 0
