from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from urbenmend.identity.models import PasswordResetToken
from urbenmend.identity.tests.factories import AdminFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_forgot_is_generic_and_sends_only_for_verified_active_email(
    django_capture_on_commit_callbacks,
) -> None:
    user = UserFactory(email="reset@example.test", email_verified_at=timezone.now())
    url = reverse("api:password-forgot")
    with django_capture_on_commit_callbacks(execute=True):
        known = APIClient().post(url, {"identifier": user.email}, format="json")
        unknown = APIClient().post(url, {"identifier": "missing@example.test"}, format="json")
    assert known.status_code == unknown.status_code == 202
    assert known.data is None and unknown.data is None
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [user.email]
    token = mail.outbox[0].body.rsplit(": ", 1)[1]
    assert token not in PasswordResetToken.objects.get(user=user).token_hash


def test_reset_changes_password_consumes_token_and_rejects_replay(
    django_capture_on_commit_callbacks,
) -> None:
    user = UserFactory(email="complete-reset@example.test", email_verified_at=timezone.now())
    forgot = reverse("api:password-forgot")
    reset = reverse("api:password-reset")
    with django_capture_on_commit_callbacks(execute=True):
        APIClient().post(forgot, {"identifier": user.email}, format="json")
    token = mail.outbox[0].body.rsplit(": ", 1)[1]
    body = {"resetToken": token, "newPassword": "A-new-password-2026!"}
    assert APIClient().post(reset, body, format="json").status_code == 200
    user.refresh_from_db()
    assert user.check_password("A-new-password-2026!")
    assert PasswordResetToken.objects.get(user=user).consumed_at is not None
    assert APIClient().post(reset, body, format="json").status_code == 422


def test_invalid_token_weak_password_and_unknown_fields_are_rejected() -> None:
    reset = reverse("api:password-reset")
    assert (
        APIClient()
        .post(reset, {"resetToken": "x" * 32, "newPassword": "weakpass"}, format="json")
        .status_code
        == 422
    )
    forgot = reverse("api:password-forgot")
    assert (
        APIClient()
        .post(forgot, {"identifier": "a@example.test", "extra": True}, format="json")
        .status_code
        == 400
    )


def test_provisioned_authority_can_verify_email_set_first_password_and_login(
    django_capture_on_commit_callbacks,
) -> None:
    admin_client = APIClient()
    admin_client.force_authenticate(AdminFactory())
    with django_capture_on_commit_callbacks(execute=True):
        provisioned = admin_client.post(
            reverse("api:users-authorities"),
            {"email": "new-authority@example.test", "categoryScope": ["roads"]},
            format="json",
        )
    assert provisioned.status_code == 201
    verification_code = mail.outbox[-1].body.rsplit(": ", 1)[1]

    verified = APIClient().post(
        reverse("api:auth-verify"),
        {
            "identifier": "new-authority@example.test",
            "channel": "email",
            "code": verification_code,
        },
        format="json",
    )
    assert verified.status_code == 200

    with django_capture_on_commit_callbacks(execute=True):
        forgot = APIClient().post(
            reverse("api:password-forgot"),
            {"identifier": "new-authority@example.test"},
            format="json",
        )
    assert forgot.status_code == 202
    reset_token = mail.outbox[-1].body.rsplit(": ", 1)[1]
    password = "Authority-first-password-2026!"
    assert (
        APIClient().post(
            reverse("api:password-reset"),
            {"resetToken": reset_token, "newPassword": password},
            format="json",
        ).status_code
        == 200
    )
    login = APIClient().post(
        reverse("api:auth-login"),
        {"identifier": "new-authority@example.test", "password": password},
        format="json",
    )
    assert login.status_code == 200
    assert login.data["user"]["role"] == "authority"
