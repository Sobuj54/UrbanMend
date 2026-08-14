"""`factory_boy` factories for the T4.1 Issue aggregate."""

from __future__ import annotations

import factory
from django.contrib.gis.geos import Point

from urbenmend.classification.models import Category
from urbenmend.issues.models import Issue, IssueStatus
from urbenmend.reporting.models import SeveritySignal

DEFAULT_ISSUE_LOCATION = Point(90.4125, 23.8103, srid=4326)


class IssueFactory(factory.django.DjangoModelFactory[Issue]):
    """One newly formed Issue using the controlled production taxonomy."""

    class Meta:
        model = Issue

    primary_category = factory.LazyFunction(lambda: Category.objects.get(slug="roads"))
    representative_location = DEFAULT_ISSUE_LOCATION
    computed_severity = SeveritySignal.MEDIUM
    computed_severity_rationale = "Highest severity signal among the member Reports."
    status = IssueStatus.SUBMITTED
