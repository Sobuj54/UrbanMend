"""Regression guards for public response PII and internal-field boundaries."""

import pytest
from django.test import Client
from django.urls import reverse

from urbenmend.identity import services as identity_services
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.issues.models import Comment, CommentVisibility
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.notifications.models import Notification
from urbenmend.notifications.services import generate_status_change_notifications
from urbenmend.notifications.tests.test_tasks import _pending_event
from urbenmend.reporting.tests.factories import ReportFactory

pytestmark = pytest.mark.django_db


def test_public_report_contains_opaque_author_id_but_no_contact_fields() -> None:
    author = UserFactory.create(email="private@example.test", phone="+8801700000000")
    report = ReportFactory.create(author=author)

    response = Client().get(reverse("api:reports-detail", kwargs={"report_id": report.pk}))

    assert response.status_code == 200
    payload = response.json()
    assert payload["authorId"] == str(author.pk)
    assert "email" not in payload
    assert "phone" not in payload
    assert "email" not in response.content.decode()
    assert "+8801700000000" not in response.content.decode()


def test_public_issue_excludes_internal_comments_and_override_reason() -> None:
    issue = IssueFactory.create(severity_override_reason="Private officer deliberation")
    Comment.objects.create(
        issue=issue,
        author=UserFactory.create(),
        body="Internal operational note",
        visibility=CommentVisibility.INTERNAL,
    )

    response = Client().get(reverse("api:issues-detail", kwargs={"issue_id": issue.pk}))

    assert response.status_code == 200
    payload = response.json()
    assert all(comment["body"] != "Internal operational note" for comment in payload["comments"])
    assert "Private officer deliberation" not in response.content.decode()


def test_deleted_report_author_is_excluded_from_historical_outbox_replay() -> None:
    event = _pending_event()
    issue = IssueFactory._meta.model.objects.get(pk=event.aggregate_id)
    author = UserFactory.create(email="deleted-private@example.test")
    ReportFactory.create(issue=issue, author=author)

    identity_services.anonymize_account(user=author)
    generate_status_change_notifications(event)

    author.refresh_from_db()
    assert author.email is None
    assert Notification.objects.filter(source_event=event, recipient=author).count() == 0
