"""T2.1 — the `Report` entity's shape and its two derived properties (FR-5, data-model §2).

Validation rules live in `test_services.py`; this module asserts the guarantees the *schema*
makes, which is where a "harmless" model edit would break them silently.

[doc: data-model §2; BR-1, BR-9; C-14]
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from urbenmend.identity.tests.factories import UserFactory
from urbenmend.reporting.models import (
    SEVERITY_RANK,
    ClassificationSource,
    Report,
    ReportStatus,
    SeveritySignal,
)
from urbenmend.reporting.tests.factories import (
    DEFAULT_LOCATION,
    ClassifiedReportFactory,
    ReportFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------------------
# The Report never carries resolution status (data-model §2 "Lifecycle")
# ---------------------------------------------------------------------------------------
def test_report_status_holds_no_issue_workflow_value() -> None:
    """⚠️ The single most consequential invariant in this model.

    `Acknowledged`, `In Progress`, `Resolved` and `Closed` are **Issue** statuses (PRD §6.3).
    Adding any of them here would give two rows one answer to "is this fixed?" and let them
    disagree — and it is a one-line, entirely plausible edit. Asserted by name, not by count, so
    the failure message says which value was added.
    """
    forbidden = {"acknowledged", "in_progress", "resolved", "closed", "rejected", "duplicate"}

    assert forbidden.isdisjoint({value for value, _ in ReportStatus.choices})


def test_report_has_no_severity_or_assignment_column() -> None:
    """Severity/assignment live on the Issue (PRD §6.1). The Report carries only a *signal*.

    `severity_signal` is the classification output (DM-A6); a bare `severity`, or any
    `assigned_to`, would move Issue-owned state onto the Report.
    """
    field_names = {field.name for field in Report._meta.get_fields()}

    assert "severity_signal" in field_names
    assert "severity" not in field_names
    assert "assigned_to" not in field_names
    assert "resolution" not in field_names


def test_report_issue_membership_is_nullable_and_single_valued() -> None:
    """BR-6/C-5: clustering may be pending, and a Report belongs to at most one Issue."""
    field = Report._meta.get_field("issue")

    assert field.null is True
    assert field.many_to_one is True
    assert field.many_to_many is False


# ---------------------------------------------------------------------------------------
# Severity bands
# ---------------------------------------------------------------------------------------
def test_severity_has_exactly_four_bands() -> None:
    """C-1/Q2 RESOLVED — four bands. `03-data-model.md` §3 and BR-8 still show a stale three."""
    assert [value for value, _ in SeveritySignal.choices] == [
        "critical",
        "high",
        "medium",
        "low",
    ]


def test_severity_rank_covers_every_band_and_orders_them() -> None:
    """BR-11's "highest severity among member reports" must be computable for every value.

    A band missing from `SEVERITY_RANK` makes `max()` raise `KeyError` inside T4.6's clustering —
    at runtime, on whichever report first carried it.
    """
    assert set(SEVERITY_RANK) == set(SeveritySignal.values)
    assert (
        SEVERITY_RANK[SeveritySignal.CRITICAL]
        > SEVERITY_RANK[SeveritySignal.HIGH]
        > SEVERITY_RANK[SeveritySignal.MEDIUM]
        > SEVERITY_RANK[SeveritySignal.LOW]
    )


# ---------------------------------------------------------------------------------------
# Author (BR-1, C-14)
# ---------------------------------------------------------------------------------------
def test_author_is_required() -> None:
    """BR-1 + Q4 RESOLVED — anonymous reporting is not supported, so `author` is never null.

    ⚠️ The `type: ignore` is the point, not a workaround: mypy rejecting `author=None` is the
    *first* line of defence, and this test asserts the second. Typing does not cover raw SQL, a
    data migration, or `**kwargs` assembled from a request body, so the `NOT NULL` constraint has
    to hold on its own.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        Report.objects.create(
            author=None,  # type: ignore[misc]
            description="A description long enough to pass.",
            location=DEFAULT_LOCATION,
        )


def test_deleting_an_author_is_protected_not_cascaded() -> None:
    """⚠️ `PROTECT` — C-14 says deleting a user must neither orphan nor destroy Issue history.

    BR-33 implements deletion as anonymization, so this cascade should never fire in production.
    The assertion is that a hard delete fails **loudly** instead of quietly erasing the reports
    that give an Issue its corroboration count (FR-16).
    """
    from django.db.models import ProtectedError

    report = ReportFactory.create()

    with pytest.raises(ProtectedError):
        report.author.delete()

    assert Report.objects.filter(pk=report.pk).exists()


def test_anonymizing_an_author_keeps_the_report() -> None:
    """The path C-14 actually takes: the user row survives with PII nulled, reports intact."""
    report = ReportFactory.create()
    author = report.author

    author.email = None
    author.status = "deleted"
    author.save()
    report.refresh_from_db()

    assert report.author_id == author.pk


# ---------------------------------------------------------------------------------------
# Classification block (BR-9)
# ---------------------------------------------------------------------------------------
def test_a_report_is_valid_before_classification() -> None:
    """BR-9 — "a Report validly exists before it is classified"."""
    report = ReportFactory.create()

    assert report.category is None
    assert report.severity_signal is None
    assert report.classification_source is None
    assert report.classified_at is None
    assert report.is_classified is False


def test_is_classified_keys_on_classified_at_not_category() -> None:
    """⚠️ A citizen's category *hint* populates `category` at intake (API §6.3).

    Keying `is_classified` on `category is not None` would report an unclassified report as
    classified, and T3.5's worker would skip it — the report would never be triaged, and nothing
    would log an error.
    """
    from urbenmend.classification.models import Category

    report = ReportFactory.create(
        category=Category.objects.get(slug="roads"),
        classification_source=ClassificationSource.CITIZEN,
    )

    assert report.category is not None
    assert report.is_classified is False

    report.classified_at = timezone.now()

    assert report.is_classified is True


def test_classified_report_carries_its_provenance() -> None:
    """FR-10/FR-15 — severity must be explainable, so model and rationale are retained."""
    report = ClassifiedReportFactory.create()

    assert report.classification_source == ClassificationSource.LLM
    assert report.classification_model
    assert report.classification_rationale
    assert report.is_classified is True


# ---------------------------------------------------------------------------------------
# is_editable (FR-11)
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "editable"),
    [
        (ReportStatus.SUBMITTED, True),
        (ReportStatus.PROCESSING, True),
        (ReportStatus.TRIAGED, False),
        (ReportStatus.HIDDEN, False),
        (ReportStatus.REMOVED, False),
    ],
)
def test_is_editable_is_pre_triage_only(status: str, editable: bool) -> None:
    """FR-11 / API §6.3 `409 NOT_EDITABLE`.

    ⚠️ `HIDDEN`/`REMOVED` are false as well as `TRIAGED`: a moderated report must not become
    editable by its author, or FR-31 removal is undoable by the person who caused it.
    """
    assert ReportFactory.build(status=status).is_editable is editable


# ---------------------------------------------------------------------------------------
# Timestamps and ordering
# ---------------------------------------------------------------------------------------
def test_created_at_is_server_set_and_default_order_is_newest_first() -> None:
    """FR-5 + API §6.3's default `-createdAt`.

    Server-authoritative because a client-supplied timestamp lets a submitter backdate a report
    to win an age-based sort (FR-19).
    """
    author = UserFactory.create()
    first = ReportFactory.create(author=author)
    second = ReportFactory.create(author=author)

    assert first.created_at is not None
    assert list(Report.objects.all()) == [second, first]


def test_str_names_the_primary_key() -> None:
    report = ReportFactory.create()

    assert str(report) == f"Report {report.pk}"
