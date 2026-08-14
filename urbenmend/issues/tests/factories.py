"""`factory_boy` factories for Issues, clustering rules and confirmations."""

from __future__ import annotations

import factory
from django.contrib.gis.geos import Point

from urbenmend.classification.models import Category
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.issues.models import (
    ClusteringRule,
    ClusteringRuleStatus,
    Confirmation,
    Issue,
    IssueStatus,
)
from urbenmend.reporting.models import SeveritySignal

DEFAULT_ISSUE_LOCATION = Point(90.4125, 23.8103, srid=4326)


class ClusteringRuleFactory(factory.django.DjangoModelFactory[ClusteringRule]):
    """A tunable rule attached to a controlled taxonomy category."""

    class Meta:
        model = ClusteringRule

    category = factory.LazyFunction(lambda: Category.objects.get(slug="roads"))
    radius_m = 50
    time_window_hours = 72
    status = ClusteringRuleStatus.ACTIVE


class IssueFactory(factory.django.DjangoModelFactory[Issue]):
    """One newly formed Issue using the controlled production taxonomy."""

    class Meta:
        model = Issue

    primary_category = factory.LazyFunction(lambda: Category.objects.get(slug="roads"))
    representative_location = DEFAULT_ISSUE_LOCATION
    computed_severity = SeveritySignal.MEDIUM
    computed_severity_rationale = "Highest severity signal among the member Reports."
    status = IssueStatus.SUBMITTED


class ConfirmationFactory(factory.django.DjangoModelFactory[Confirmation]):
    """One citizen's revocable endorsement of an Issue."""

    class Meta:
        model = Confirmation

    issue = factory.SubFactory(IssueFactory)
    citizen = factory.SubFactory(UserFactory)
