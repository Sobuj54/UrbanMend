"""T5.5 - scoped, explainable Issue severity overrides."""

from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from urbenmend.api.exceptions import UnprocessableEntity
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.issues.models import Issue
from urbenmend.issues.services import override_issue_severity
from urbenmend.issues.tests.factories import IssueFactory
from urbenmend.reporting.models import SeveritySignal

pytestmark = pytest.mark.django_db


def _scoped_authority(issue: Issue) -> User:
    authority = AuthorityFactory.create()
    authority.category_scope.add(issue.primary_category)
    return authority


def _url(issue_id: uuid.UUID) -> str:
    return reverse("api:issues-severity", kwargs={"issue_id": issue_id})


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_authority_override_preserves_computed_severity_and_records_evidence() -> None:
    issue = IssueFactory.create(
        computed_severity=SeveritySignal.HIGH,
        computed_severity_rationale="Traffic collision risk.",
    )
    actor = _scoped_authority(issue)

    result = override_issue_severity(
        actor=actor,
        issue_id=issue.pk,
        severity=SeveritySignal.LOW,
        reason="  Inspection found only cosmetic damage.  ",
    )

    issue.refresh_from_db()
    assert issue.computed_severity == SeveritySignal.HIGH
    assert issue.computed_severity_rationale == "Traffic collision risk."
    assert issue.overridden_severity == SeveritySignal.LOW
    assert issue.severity_override_reason == "Inspection found only cosmetic damage."
    assert issue.severity_overridden_by == actor
    assert issue.severity_overridden_at == result.overridden_at
    assert result.current == SeveritySignal.LOW


def test_admin_can_replace_an_existing_override_without_scope() -> None:
    issue = IssueFactory.create(
        overridden_severity=SeveritySignal.LOW,
        severity_override_reason="First review.",
    )
    admin = AdminFactory.create()

    result = override_issue_severity(
        actor=admin,
        issue_id=issue.pk,
        severity=SeveritySignal.CRITICAL,
        reason="Live electrical wire confirmed.",
    )

    assert result.overridden == SeveritySignal.CRITICAL
    assert result.overridden_by == admin.pk


@pytest.mark.parametrize(
    ("severity", "reason"),
    [
        (None, "Evidence."),
        ("urgent", "Evidence."),
        (SeveritySignal.HIGH, None),
        (SeveritySignal.HIGH, "  "),
    ],
)
def test_invalid_band_or_reason_is_unprocessable(severity: str | None, reason: str | None) -> None:
    issue = IssueFactory.create()
    with pytest.raises(UnprocessableEntity):
        override_issue_severity(
            actor=AdminFactory.create(),
            issue_id=issue.pk,
            severity=severity,
            reason=reason,
        )
    issue.refresh_from_db()
    assert issue.overridden_severity is None


def test_role_and_category_scope_are_enforced() -> None:
    issue = IssueFactory.create()
    with pytest.raises(PermissionDenied):
        override_issue_severity(
            actor=UserFactory.create(),
            issue_id=issue.pk,
            severity=SeveritySignal.HIGH,
            reason="Citizen cannot override.",
        )
    with pytest.raises(PermissionDenied):
        override_issue_severity(
            actor=AuthorityFactory.create(),
            issue_id=issue.pk,
            severity=SeveritySignal.HIGH,
            reason="Out of scope.",
        )


def test_endpoint_returns_computed_and_override_state() -> None:
    issue = IssueFactory.create(computed_severity=SeveritySignal.MEDIUM)
    actor = _scoped_authority(issue)

    response = _client_for(actor).patch(
        _url(issue.pk),
        data={"severity": "high", "reason": "School entrance is obstructed."},
        content_type="application/json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["issueId"] == str(issue.pk)
    assert body["severity"]["computed"] == "medium"
    assert body["severity"]["overridden"] == "high"
    assert body["severity"]["current"] == "high"
    assert body["severity"]["overrideReason"] == "School entrance is obstructed."
    assert body["severity"]["overriddenBy"] == str(actor.pk)
    assert body["severity"]["overriddenAt"] is not None


def test_endpoint_returns_422_for_business_rules_and_400_for_unknown_fields() -> None:
    issue = IssueFactory.create()
    client = _client_for(_scoped_authority(issue))

    assert (
        client.patch(
            _url(issue.pk), data={"severity": "urgent"}, content_type="application/json"
        ).status_code
        == 422
    )
    unknown = client.patch(
        _url(issue.pk),
        data={"severity": "high", "reason": "Evidence.", "score": 99},
        content_type="application/json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["details"][0]["field"] == "score"


def test_endpoint_requires_authentication_and_csrf() -> None:
    issue = IssueFactory.create()
    body = {"severity": "high", "reason": "Evidence."}
    assert (
        Client().patch(_url(issue.pk), data=body, content_type="application/json").status_code
        == 401
    )

    client = Client(enforce_csrf_checks=True)
    client.force_login(_scoped_authority(issue))
    assert (
        client.patch(_url(issue.pk), data=body, content_type="application/json").status_code == 403
    )
