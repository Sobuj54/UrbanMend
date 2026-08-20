"""T6.4 - self-owned notification read APIs."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationState,
    NotificationType,
    OutboxEvent,
)

pytestmark = pytest.mark.django_db


def _notification(*, recipient: User, read: bool = False) -> Notification:
    issue = IssueFactory.create()
    event = OutboxEvent.objects.create(
        event_type="issue.status_changed",
        aggregate_type="issue",
        aggregate_id=issue.pk,
        payload={},
    )
    return Notification.objects.create(
        recipient=recipient,
        issue=issue,
        source_event=event,
        notification_type=NotificationType.ISSUE_STATUS_CHANGED,
        channel=NotificationChannel.IN_APP,
        body="Your reported issue status changed from triaged to acknowledged.",
        state=NotificationState.DELIVERED,
        read_at=timezone.now() if read else None,
    )


def _collection_url() -> str:
    return reverse("api:notifications")


def _detail_url(notification: Notification) -> str:
    return reverse("api:notifications-detail", kwargs={"notification_id": notification.pk})


def test_notification_collection_requires_authentication() -> None:
    assert Client().get(_collection_url()).status_code == 401


def test_notification_stream_requires_authentication() -> None:
    response = Client().get(reverse("api:notifications-stream"))
    assert response.status_code == 401


def test_notification_stream_emits_owned_notification() -> None:
    owner = UserFactory.create()
    notification = _notification(recipient=owner)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("api:notifications-stream"))

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    assert f'"notificationId": "{notification.pk}"' in b"".join(response.streaming_content).decode()


def test_list_returns_only_callers_notifications_in_standard_envelope() -> None:
    owner = UserFactory.create()
    own = _notification(recipient=owner)
    _notification(recipient=UserFactory.create())
    client = Client()
    client.force_login(owner)

    response = client.get(_collection_url())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "page", "meta"}
    assert body["meta"] == {"count": 1}
    assert body["data"] == [
        {
            "id": str(own.pk),
            "type": "issue_status_changed",
            "issueId": str(own.issue_id),
            "body": own.body,
            "channel": "in_app",
            "read": False,
            "createdAt": own.created_at.isoformat().replace("+00:00", "Z"),
        }
    ]


def test_list_filters_unread_state() -> None:
    owner = UserFactory.create()
    unread = _notification(recipient=owner)
    _notification(recipient=owner, read=True)
    client = Client()
    client.force_login(owner)

    response = client.get(_collection_url(), {"unread": "true"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [str(unread.pk)]


def test_list_rejects_unknown_filter() -> None:
    client = Client()
    client.force_login(UserFactory.create())

    response = client.get(_collection_url(), {"status": "pending"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_mark_read_is_idempotent_and_returns_resource() -> None:
    owner = UserFactory.create()
    notification = _notification(recipient=owner)
    client = Client()
    client.force_login(owner)

    first = client.patch(
        _detail_url(notification), data={"read": True}, content_type="application/json"
    )
    notification.refresh_from_db()
    first_read_at = notification.read_at
    second = client.patch(
        _detail_url(notification), data={"read": True}, content_type="application/json"
    )
    notification.refresh_from_db()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["read"] is True
    assert notification.read_at == first_read_at


def test_mark_read_hides_other_users_notification() -> None:
    notification = _notification(recipient=UserFactory.create())
    client = Client()
    client.force_login(UserFactory.create())

    response = client.patch(
        _detail_url(notification), data={"read": True}, content_type="application/json"
    )

    assert response.status_code == 404


def test_mark_read_rejects_false() -> None:
    owner = UserFactory.create()
    notification = _notification(recipient=owner)
    client = Client()
    client.force_login(owner)

    response = client.patch(
        _detail_url(notification), data={"read": False}, content_type="application/json"
    )

    assert response.status_code == 400
    notification.refresh_from_db()
    assert notification.read_at is None


def test_mark_all_read_affects_only_callers_notifications() -> None:
    owner = UserFactory.create()
    own_first = _notification(recipient=owner)
    own_second = _notification(recipient=owner)
    other = _notification(recipient=UserFactory.create())
    client = Client()
    client.force_login(owner)

    response = client.post(
        reverse("api:notifications-read-all"), data={}, content_type="application/json"
    )

    assert response.status_code == 204
    for notification in (own_first, own_second, other):
        notification.refresh_from_db()
    assert own_first.read_at is not None
    assert own_second.read_at is not None
    assert other.read_at is None
