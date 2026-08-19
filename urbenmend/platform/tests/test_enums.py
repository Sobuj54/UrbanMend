import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from urbenmend.classification.models import Category
from urbenmend.issues.models import IssueStatus
from urbenmend.notifications.models import NotificationChannel, NotificationType
from urbenmend.reporting.models import ReportStatus, SeveritySignal

pytestmark = pytest.mark.django_db


def test_enum_metadata_is_public_and_model_derived() -> None:
    response = APIClient().get(reverse("api:meta-enums"))
    assert response.status_code == 200
    assert [item["value"] for item in response.data["severities"]] == list(SeveritySignal.values)
    assert [item["value"] for item in response.data["issueStatuses"]] == list(IssueStatus.values)
    assert [item["value"] for item in response.data["reportStatuses"]] == list(ReportStatus.values)
    assert [item["value"] for item in response.data["notificationTypes"]] == list(
        NotificationType.values
    )
    assert [item["value"] for item in response.data["notificationChannels"]] == list(
        NotificationChannel.values
    )
    assert set(NotificationChannel.values) == {"in_app", "email"}


def test_categories_include_active_and_retired_taxonomy_rows() -> None:
    retired = Category.objects.create(
        slug="retired-enum-test", name_en="Retired Enum", name_bn="অবসর", status="retired"
    )
    response = APIClient().get(reverse("api:meta-enums"))
    item = next(
        category for category in response.data["categories"] if category["key"] == retired.slug
    )
    assert item == {
        "key": "retired-enum-test",
        "label": {"en": "Retired Enum", "bn": "অবসর"},
        "active": False,
    }
