from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from urbenmend.audit.services import record_event
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory

pytestmark = pytest.mark.django_db


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


def test_authentication_and_role_authorization() -> None:
    url = reverse("api:audit-events")
    assert _client().get(url).status_code == 401
    assert _client(UserFactory()).get(url).status_code == 403


def test_admin_can_filter_all_events_using_documented_query_keys() -> None:
    admin = AdminFactory()
    authority = AuthorityFactory()
    target = UserFactory()
    record_event(actor=admin, action="identity.created", target=target)
    wanted = record_event(actor=authority, action="issue.assigned", target=target)

    response = _client(admin).get(
        reverse("api:audit-events"),
        {
            "actorId": str(authority.pk),
            "action": "issue.assigned",
            "targetType": "user",
            "targetId": str(target.pk),
            "from": (timezone.now() - timedelta(minutes=1)).isoformat(),
            "to": (timezone.now() + timedelta(minutes=1)).isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.data["meta"]["count"] == 1
    assert response.data["data"] == [
        {
            "actorId": str(authority.pk),
            "action": "issue.assigned",
            "targetType": "user",
            "targetId": str(target.pk),
            "before": None,
            "after": None,
            "at": wanted.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]


def test_authority_sees_only_own_actions_even_with_actor_filter() -> None:
    first = AuthorityFactory()
    second = AuthorityFactory()
    record_event(actor=first, action="issue.assigned", target=first)
    record_event(actor=second, action="issue.assigned", target=second)

    response = _client(first).get(reverse("api:audit-events"), {"actorId": str(second.pk)})

    assert response.status_code == 200
    assert response.data["meta"]["count"] == 1
    assert response.data["data"][0]["actorId"] == str(first.pk)


def test_audit_events_are_cursor_paginated_and_reject_unknown_filters() -> None:
    admin = AdminFactory()
    for index in range(3):
        record_event(actor=admin, action=f"test.{index}", target=admin)
    url = reverse("api:audit-events")

    first = _client(admin).get(url, {"limit": 2})
    assert first.status_code == 200
    assert len(first.data["data"]) == 2
    assert first.data["page"]["nextCursor"] is not None
    second = _client(admin).get(url, {"limit": 2, "cursor": first.data["page"]["nextCursor"]})
    assert second.status_code == 200
    assert len(second.data["data"]) == 1
    assert _client(admin).get(url, {"unknown": "x"}).status_code == 400
