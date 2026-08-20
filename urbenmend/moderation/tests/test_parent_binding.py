import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.identity.tests.factories import AdminFactory, UserFactory
from urbenmend.issues.models import Comment
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def test_comment_moderation_requires_matching_parent_issue() -> None:
    admin = AdminFactory.create()
    author = UserFactory.create()
    actual_issue = IssueFactory.create()
    other_issue = IssueFactory.create()
    comment = Comment.objects.create(
        issue=actual_issue, author=author, body="Keep this", visibility="public"
    )
    client = APIClient()
    client.force_authenticate(admin)
    url = reverse(
        "api:comments-moderation",
        kwargs={"pk": other_issue.pk, "comment_id": comment.pk},
    )

    response = client.post(url, {"action": "remove", "reason": "test"}, format="json")

    comment.refresh_from_db()
    assert response.status_code == 404
    assert comment.removed_at is None
