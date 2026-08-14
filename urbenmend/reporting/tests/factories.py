"""`factory_boy` factories for `reporting` (T2.1).

[doc: testing.md "factory_boy"; data-model §2; BR-1, BR-2, BR-9]
"""

from __future__ import annotations

from typing import Any

import factory
from django.contrib.gis.geos import Point
from django.utils import timezone

from urbenmend.classification.models import Category
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus, SeveritySignal

# Central Dhaka — inside `docs/city-boundary/dhaka-demo.geojson`, so a factory-built Report is
# consistent with the boundary the migration seeds. Matches `geo.tests.factories.INSIDE_POINT`;
# not imported from there because a `reporting` fixture depending on `geo`'s test package would
# invert the app dependency direction for no gain.
DEFAULT_LOCATION = Point(90.4125, 23.8103, srid=4326)


class ReportFactory(factory.django.DjangoModelFactory[Report]):
    """One citizen submission, **unclassified** — the state `POST /reports` leaves behind.

    ⚠️ **Unclassified is the default because BR-9 says a Report validly exists before it is
    classified.** `category`, `severity_signal` and `classified_at` are all null and `status` is
    `submitted`. A factory that pre-filled them would make the async triage path (T3.5) untestable
    from its real starting state, and every "worker picks up unclassified work" query would return
    nothing while looking correct.

    ⚠️ **Builds through `objects.create()`, bypassing `create_report()` — on purpose.** The
    service is the thing under test in `test_services.py`; routing fixtures through it would mean
    a validation bug makes unrelated suites fail, and BR-35 would then require every test to seed
    a boundary first. Tests that assert *intake* call the service directly.
    """

    class Meta:
        model = Report

    author = factory.SubFactory(UserFactory)
    # Long enough to satisfy `MIN_DESCRIPTION_LENGTH` if a test does pass it to the service, so
    # the default fixture is not incidentally invalid.
    description = factory.Sequence(lambda n: f"Large pothole blocking the lane, report {n}.")
    location = DEFAULT_LOCATION
    address = ""
    language = "en"
    status = ReportStatus.SUBMITTED
    # ⚠️ Annotated `Any` because `ClassifiedReportFactory` overrides all five with real values.
    # Inferred from `None` they would be `None`-typed, and every subclass assignment below becomes
    # an `[assignment]` error — a complaint about the base class's *placeholder*, not about the
    # override. The declared type has to be the union of "unset" and "whatever triage produced",
    # which for a factory declaration (a value, a `LazyFunction`, or a `SubFactory`) is `Any`.
    category: Any = None
    severity_signal: Any = None
    confidence: Any = None
    classification_source: Any = None
    classification_model = ""
    classification_rationale = ""
    classified_at: Any = None


class ClassifiedReportFactory(ReportFactory):
    """A Report after async triage — what clustering (T4.4) and the Issue tests consume.

    ⚠️ **`category` is a real seeded slug, not a factory-built node.** C-2 makes the taxonomy
    controlled, and clustering keys on category, so a test node would cluster against nothing the
    product ships.

    ⚠️ **`status` stays `processing`, not `triaged`.** `triaged` means "attached to an Issue"
    (data-model §2), and this pre-clustering factory has no Issue — asserting otherwise
    would ship a row whose status contradicts its (absent) Issue FK.
    """

    status = ReportStatus.PROCESSING
    # `LazyFunction`, not a module-level constant: the query must run inside the test's
    # transaction, after migrations have seeded the taxonomy. Evaluating it at import time
    # would hit the database while pytest is still collecting.
    category = factory.LazyFunction(lambda: Category.objects.get(slug="roads"))
    severity_signal = SeveritySignal.MEDIUM
    confidence = 0.82
    classification_source = ClassificationSource.LLM
    classification_model = "test-model/v0"
    classification_rationale = "Seeded by ClassifiedReportFactory for tests."
    classified_at = factory.LazyFunction(timezone.now)
