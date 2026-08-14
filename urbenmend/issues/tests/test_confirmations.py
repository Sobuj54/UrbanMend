"""T4.7 revocable confirmations and distinct-reporter corroboration."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.http import Http404
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from urbenmend.api.exceptions import Conflict
from urbenmend.classification.tests.factories import CategoryFactory
from urbenmend.identity.models import User, UserStatus
from urbenmend.identity.tests.factories import AuthorityFactory, UserFactory
from urbenmend.issues.models import Confirmation, IssueStatus
from urbenmend.issues.services import confirm_issue, withdraw_confirmation
from urbenmend.issues.tests.factories import ConfirmationFactory, IssueFactory
from urbenmend.reporting.tests.factories import ClassifiedReportFactory

pytestmark = pytest.mark.django_db


def _verified_citizen() -> User:
    return UserFactory.create(email_verified_at=timezone.now())


def _create_url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-confirmations", kwargs={"issue_id": issue_id})


def _delete_url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-confirmations-me", kwargs={"issue_id": issue_id})


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _confirm_in_thread(*, actor_id: uuid.UUID, issue_id: uuid.UUID) -> str:
    """Use one connection per worker so the Issue row lock is exercised for real."""
    close_old_connections()
    try:
        confirm_issue(actor=User.objects.get(pk=actor_id), issue_id=issue_id)
        return "created"
    except Conflict as exc:
        assert exc.get_codes() == "ALREADY_CONFIRMED"
        return "conflict"
    finally:
        connection.close()


def test_database_allows_only_one_confirmation_per_citizen_and_issue() -> None:
    confirmation = ConfirmationFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        Confirmation.objects.create(
            issue=confirmation.issue,
            citizen=confirmation.citizen,
        )


def test_corroboration_counts_distinct_active_reporters() -> None:
    issue = IssueFactory.create()
    first = _verified_citizen()
    second = _verified_citizen()
    ClassifiedReportFactory.create_batch(2, issue=issue, author=first)
    ConfirmationFactory.create(issue=issue, citizen=first)
    ConfirmationFactory.create(issue=issue, citizen=second)

    assert issue.corroboration_count == 2
    assert "corroboration_count" not in {field.name for field in issue._meta.fields}


def test_inactive_accounts_do_not_inflate_corroboration() -> None:
    issue = IssueFactory.create()
    trusted = _verified_citizen()
    unverified = UserFactory.create()
    suspended = UserFactory.create(
        email_verified_at=timezone.now(),
        status=UserStatus.SUSPENDED,
    )
    ClassifiedReportFactory.create(issue=issue, author=trusted)
    ConfirmationFactory.create(issue=issue, citizen=unverified)
    ConfirmationFactory.create(issue=issue, citizen=suspended)

    assert issue.corroboration_count == 2


def test_confirm_issue_creates_one_confirmation_and_returns_the_derived_count() -> None:
    issue = IssueFactory.create()
    reporter = _verified_citizen()
    actor = _verified_citizen()
    ClassifiedReportFactory.create(issue=issue, author=reporter)

    result = confirm_issue(actor=actor, issue_id=issue.pk)

    assert result.issue_id == issue.pk
    assert result.corroboration_count == 2
    assert Confirmation.objects.get(issue=issue, citizen=actor)


def test_repeated_confirmation_is_an_explicit_conflict() -> None:
    confirmation = ConfirmationFactory.create()

    with pytest.raises(Conflict) as raised:
        confirm_issue(actor=confirmation.citizen, issue_id=confirmation.issue_id)

    assert raised.value.get_codes() == "ALREADY_CONFIRMED"
    assert Confirmation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_repeat_creates_one_confirmation_and_one_conflict() -> None:
    actor = _verified_citizen()
    issue = IssueFactory.create(primary_category=CategoryFactory.create())

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(
            future.result()
            for future in [
                executor.submit(_confirm_in_thread, actor_id=actor.pk, issue_id=issue.pk),
                executor.submit(_confirm_in_thread, actor_id=actor.pk, issue_id=issue.pk),
            ]
        )

    assert outcomes == ["conflict", "created"]
    assert Confirmation.objects.filter(issue=issue, citizen=actor).count() == 1


def test_withdrawal_removes_confirmation_and_decreases_count() -> None:
    actor = _verified_citizen()
    confirmation = ConfirmationFactory.create(citizen=actor)
    assert confirmation.issue.corroboration_count == 1

    withdraw_confirmation(actor=actor, issue_id=confirmation.issue_id)

    assert Confirmation.objects.count() == 0
    assert confirmation.issue.corroboration_count == 0


def test_withdrawing_an_absent_confirmation_is_not_found() -> None:
    with pytest.raises(Http404, match="Confirmation"):
        withdraw_confirmation(actor=_verified_citizen(), issue_id=IssueFactory.create().pk)


@pytest.mark.parametrize("status", [IssueStatus.HIDDEN, IssueStatus.REMOVED])
def test_moderated_issue_cannot_be_confirmed(status: str) -> None:
    issue = IssueFactory.create(status=status)

    with pytest.raises(Http404, match="Issue"):
        confirm_issue(actor=_verified_citizen(), issue_id=issue.pk)


def test_post_confirmation_endpoint_returns_the_contract_body() -> None:
    issue = IssueFactory.create()
    reporter = _verified_citizen()
    actor = _verified_citizen()
    ClassifiedReportFactory.create(issue=issue, author=reporter)

    response = _client_for(actor).post(
        _create_url(issue.pk),
        data={},
        content_type="application/json",
    )

    assert response.status_code == 201
    assert response.json() == {
        "issueId": str(issue.pk),
        "corroborationCount": 2,
    }


def test_post_confirmation_endpoint_returns_already_confirmed() -> None:
    confirmation = ConfirmationFactory.create()

    response = _client_for(confirmation.citizen).post(
        _create_url(confirmation.issue_id),
        data={},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_CONFIRMED"


def test_post_confirmation_rejects_client_owned_count() -> None:
    issue = IssueFactory.create()

    response = _client_for(_verified_citizen()).post(
        _create_url(issue.pk),
        data={"corroborationCount": 99},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "corroborationCount"
    assert Confirmation.objects.count() == 0


def test_delete_confirmation_endpoint_is_revocable_then_not_found() -> None:
    actor = _verified_citizen()
    confirmation = ConfirmationFactory.create(citizen=actor)
    client = _client_for(actor)

    assert client.delete(_delete_url(confirmation.issue_id)).status_code == 204
    assert client.delete(_delete_url(confirmation.issue_id)).status_code == 404


def test_confirmation_endpoints_require_authentication() -> None:
    issue = IssueFactory.create()

    assert Client().post(_create_url(issue.pk)).status_code == 401
    assert Client().delete(_delete_url(issue.pk)).status_code == 401


def test_confirmation_is_citizen_only() -> None:
    issue = IssueFactory.create()

    response = _client_for(AuthorityFactory.create()).post(
        _create_url(issue.pk),
        data={},
        content_type="application/json",
    )

    assert response.status_code == 403
    assert Confirmation.objects.count() == 0


def test_confirmation_post_enforces_csrf() -> None:
    actor = _verified_citizen()
    client = Client(enforce_csrf_checks=True)
    client.force_login(actor)

    response = client.post(
        _create_url(IssueFactory.create().pk),
        data={},
        content_type="application/json",
    )

    assert response.status_code == 403
    assert Confirmation.objects.count() == 0
