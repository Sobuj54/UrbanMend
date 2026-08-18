"""Export creation, ownership, and worker generation (T9.1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from urbenmend.classification.models import Category
from urbenmend.export.models import Export, ExportState
from urbenmend.export.tasks import generate_export
from urbenmend.identity.tests.factories import AuthorityFactory, UserFactory
from urbenmend.issues.tests.factories import IssueFactory

pytestmark = pytest.mark.django_db


def test_authority_can_request_export(client: Client) -> None:
    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug="roads"))
    client.force_login(authority)

    with (
        patch(
            "urbenmend.export.services.transaction.on_commit",
            side_effect=lambda callback: callback(),
        ),
        patch("urbenmend.export.services.generate_export.delay") as enqueue,
    ):
        response = client.post(
            reverse("api:exports"),
            {"resource": "issues", "format": "csv", "filters": {"category": "roads"}},
            content_type="application/json",
        )

    assert response.status_code == 202
    export = Export.objects.get(pk=response.json()["exportId"])
    assert export.requester == authority
    assert export.state == ExportState.PROCESSING
    enqueue.assert_called_once_with(str(export.pk))


def test_citizen_cannot_request_export(client: Client) -> None:
    client.force_login(UserFactory.create())

    response = client.post(
        reverse("api:exports"),
        {"resource": "issues", "format": "csv"},
        content_type="application/json",
    )

    assert response.status_code == 403


def test_export_polling_is_creator_only(client: Client) -> None:
    owner = AuthorityFactory.create()
    other = AuthorityFactory.create()
    export = Export.objects.create(requester=owner, resource="issues", format="csv")
    client.force_login(other)

    assert (
        client.get(reverse("api:exports-detail", kwargs={"export_id": export.pk})).status_code
        == 404
    )


def test_worker_generates_scoped_issue_csv() -> None:
    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug="roads"))
    included = IssueFactory.create()
    IssueFactory.create(primary_category=Category.objects.get(slug="water_drainage"))
    export = Export.objects.create(requester=authority, resource="issues", format="csv")

    with patch("urbenmend.export.tasks.default_storage.save", return_value="stored") as save:
        generate_export(str(export.pk))

    export.refresh_from_db()
    assert export.state == ExportState.READY
    assert export.object_key == f"exports/{export.pk}.csv"
    content = save.call_args.args[1].read().decode()
    assert str(included.pk) in content
    assert content.count("\n") == 2
