import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.identity.tests.factories import AuthorityFactory
from urbenmend.issues.models import IssueStatus
from urbenmend.issues.services import transition_issue_status
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def client(user=None):
    c = APIClient()
    if user:
        c.force_authenticate(user)
    return c


def test_status_history_is_public_and_has_contract_shape() -> None:
    issue = IssueFactory(status=IssueStatus.TRIAGED)
    actor = AuthorityFactory()
    actor.category_scope.add(issue.primary_category)
    transition_issue_status(actor=actor, issue_id=issue.pk, to_status=IssueStatus.ACKNOWLEDGED)
    response = client().get(reverse("api:issues-status-events", kwargs={"issue_id": issue.pk}))
    assert response.status_code == 200
    assert response.data["meta"]["count"] == 1
    assert response.data["data"][0]["from"] == "triaged"
    assert response.data["data"][0]["to"] == "acknowledged"
    assert response.data["data"][0]["actorRole"] == "authority"
    assert response.data["data"][0]["reason"] is None


def test_authority_cannot_read_history_outside_category_scope() -> None:
    issue = IssueFactory(status=IssueStatus.TRIAGED)
    actor = AuthorityFactory()
    response = client(actor).get(reverse("api:issues-status-events", kwargs={"issue_id": issue.pk}))
    assert response.status_code == 404


def test_moderated_and_unknown_issues_do_not_leak_history() -> None:
    issue = IssueFactory(status=IssueStatus.HIDDEN)
    url = reverse("api:issues-status-events", kwargs={"issue_id": issue.pk})
    assert client().get(url).status_code == 404
    import uuid

    assert (
        client()
        .get(reverse("api:issues-status-events", kwargs={"issue_id": uuid.uuid4()}))
        .status_code
        == 404
    )
