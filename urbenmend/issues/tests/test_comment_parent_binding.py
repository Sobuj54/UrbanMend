"""Nested comment routes must bind the comment to the issue in the URL."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.identity.tests.factories import UserFactory
from urbenmend.issues.models import Comment
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_mismatched_issue_cannot_update_or_delete_comment() -> None:
    author = UserFactory.create()
    actual_issue = IssueFactory.create()
    other_issue = IssueFactory.create()
    comment = Comment.objects.create(
        issue=actual_issue,
        author=author,
        body="Original body",
        visibility="public",
    )
    url = reverse(
        "api:issues-comment-detail",
        kwargs={"issue_id": other_issue.pk, "comment_id": comment.pk},
    )

    patch_response = _client(author).patch(url, {"body": "Changed"}, format="json")
    delete_response = _client(author).delete(url)

    comment.refresh_from_db()
    assert patch_response.status_code == 404
    assert delete_response.status_code == 404
    assert comment.body == "Original body"
    assert comment.removed_at is None
