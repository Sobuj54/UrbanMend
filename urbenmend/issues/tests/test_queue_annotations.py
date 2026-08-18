"""Queue annotations ⇄ model properties (T7.1, FR-16, FR-22).

`Issue.current_severity`, `report_count` and `corroboration_count` are read-only Python properties.
`?sort=severity` and `?sort=corroborationCount` need the same three numbers in `ORDER BY` *and* in
the keyset cursor's `WHERE`, which a property cannot serve — so `annotate_queue_fields()` restates
them in SQL.

**Two restatements of one rule is the risk this module exists for.** They agree the day they are
written. The dedupe (`Count(distinct=True)`), the active-status filter and the override precedence
are each one edit away from disagreeing, and the divergence is invisible: the list renders the
annotation, and every other read renders the property, so a client sees `corroborationCount: 4` on
`GET /issues` and `3` everywhere else with nothing erroring anywhere.

[doc: API §6.5; FR-16, FR-21, FR-22, BR-22]
"""

from __future__ import annotations

import pytest

from urbenmend.identity.models import UserStatus
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.issues.models import ACTIVE_CORROBORATION_STATUSES, Issue
from urbenmend.issues.selectors import annotate_queue_fields
from urbenmend.issues.tests.factories import ConfirmationFactory, IssueFactory
from urbenmend.reporting.models import SEVERITY_RANK, ReportStatus, SeveritySignal
from urbenmend.reporting.tests.factories import ReportFactory

pytestmark = pytest.mark.django_db


def _annotated(issue: Issue) -> Issue:
    """The same row, re-read through the queue annotations.

    ⚠️ **Re-fetched rather than annotated in place.** The annotation and the property must be read
    off *separate* instances or a stale in-memory `Issue` could make a disagreement look like
    agreement — and re-fetching is also what proves the alias names do not collide with the
    properties: Django `setattr`s every annotation onto the instance, and a property with no setter
    turns that into an `AttributeError` on the first row fetched.
    """
    return annotate_queue_fields(Issue.objects.filter(pk=issue.pk)).get()


def _assert_parity(issue: Issue) -> None:
    """Both derived counts, from both sides, for one row."""
    annotated = _annotated(issue)
    assert annotated.corroboration_total == issue.corroboration_count  # type: ignore[attr-defined]
    assert annotated.report_total == issue.report_count  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Corroboration parity — FR-16's "distinct people"
# --------------------------------------------------------------------------------------


def test_an_issue_with_no_members_counts_zero_rather_than_null() -> None:
    """⚠️ **The `Coalesce` around each `Subquery`, asserted.** A correlated aggregate over zero rows
    returns no row at all, so the annotation is `NULL` — and `NULL` in `ORDER BY` sorts to one end
    regardless of direction, which would park every empty Issue at the top or bottom of
    `?sort=corroborationCount` forever. It also serializes as `null` where §6.5 promises a number.
    """
    issue = IssueFactory.create()

    annotated = _annotated(issue)

    assert annotated.corroboration_total == 0  # type: ignore[attr-defined]
    assert annotated.report_total == 0  # type: ignore[attr-defined]
    _assert_parity(issue)


def test_a_reporter_corroborates_their_own_issue() -> None:
    issue = IssueFactory.create()
    ReportFactory.create(issue=issue)

    assert _annotated(issue).corroboration_total == 1  # type: ignore[attr-defined]
    _assert_parity(issue)


def test_a_confirmer_who_never_reported_still_corroborates() -> None:
    """BR-22: confirming is the other half of FR-16's count. A subquery that joined only `reports`
    would answer `1` for a widely-confirmed Issue and look entirely plausible."""
    issue = IssueFactory.create()
    ReportFactory.create(issue=issue)
    ConfirmationFactory.create(issue=issue)

    assert _annotated(issue).corroboration_total == 2  # type: ignore[attr-defined]
    _assert_parity(issue)


def test_one_person_who_reported_and_confirmed_counts_once() -> None:
    """⚠️ **The case that makes this module a test rather than a formality.**

    The subquery's `OR` spans two different joins, so a user who both reported *and* confirmed
    matches twice and `Count("pk")` would return 2 for one person. FR-16 counts *distinct people* —
    inflating it turns corroboration into a number an author can raise by themselves, which is
    exactly the trust signal it is supposed to be.
    """
    issue = IssueFactory.create()
    citizen = UserFactory.create()
    ReportFactory.create(issue=issue, author=citizen)
    ConfirmationFactory.create(issue=issue, citizen=citizen)

    assert issue.corroboration_count == 1
    assert _annotated(issue).corroboration_total == 1  # type: ignore[attr-defined]


def test_several_reports_from_one_person_are_one_voice() -> None:
    """`reportCount` and `corroborationCount` are different numbers, and this is the fixture that
    separates them: three submissions from one citizen are three Reports and one corroborator."""
    issue = IssueFactory.create()
    citizen = UserFactory.create()
    ReportFactory.create_batch(3, issue=issue, author=citizen)

    annotated = _annotated(issue)

    assert annotated.report_total == 3  # type: ignore[attr-defined]
    assert annotated.corroboration_total == 1  # type: ignore[attr-defined]
    _assert_parity(issue)


@pytest.mark.parametrize("status", [UserStatus.SUSPENDED, UserStatus.DELETED])
def test_an_inactive_persons_report_does_not_corroborate(status: str) -> None:
    """⚠️ **`ACTIVE_CORROBORATION_STATUSES` is one constant read by both sides.** Restated, the two
    would agree until a `UserStatus` member was added to one of them — and the failure would be a
    corroboration count that differs between the queue and the detail read for suspended authors
    only, which no reasonable amount of manual testing surfaces."""
    issue = IssueFactory.create()
    author = UserFactory.create()
    ReportFactory.create(issue=issue, author=author)
    author.status = status
    author.save(update_fields=["status"])

    annotated = _annotated(issue)

    assert annotated.corroboration_total == 0  # type: ignore[attr-defined]
    # The Report itself is still a member: moderation and account status are different questions.
    assert annotated.report_total == 1  # type: ignore[attr-defined]
    _assert_parity(issue)


def test_a_registered_but_unverified_person_corroborates() -> None:
    """BR-30 gates *notification*, not corroboration, so `registered` is in the active set. Asserted
    because "verified users only" is a plausible-sounding tightening that would silently drop the
    majority of a fresh deployment's signal."""
    assert UserStatus.REGISTERED in ACTIVE_CORROBORATION_STATUSES
    issue = IssueFactory.create()
    ReportFactory.create(issue=issue, author=UserFactory.create(status=UserStatus.REGISTERED))

    assert _annotated(issue).corroboration_total == 1  # type: ignore[attr-defined]
    _assert_parity(issue)


@pytest.mark.parametrize("status", [ReportStatus.HIDDEN, ReportStatus.REMOVED])
def test_a_moderated_member_report_is_still_counted(status: str) -> None:
    """⚠️ **Deliberate, and the tempting "fix" changes the product.** `Issue.report_count` is a plain
    `self.reports.count()`, and this annotation exists to be the same number in SQL. Excluding
    moderated members here would make a six-report Issue render as five for reasons no client could
    see, and would quietly reduce FR-16's corroboration story. Suppressing moderated *content* is
    §6.13's job, on the Report resource — not in a count.
    """
    issue = IssueFactory.create()
    ReportFactory.create(issue=issue, status=status)

    annotated = _annotated(issue)

    assert annotated.report_total == 1  # type: ignore[attr-defined]
    assert annotated.corroboration_total == 1  # type: ignore[attr-defined]
    _assert_parity(issue)


def test_members_of_another_issue_are_not_counted() -> None:
    """The subqueries are correlated on `OuterRef("pk")`. A dropped correlation would return the
    whole table's count on every row — a number that looks like a busy city rather than a bug."""
    issue = IssueFactory.create()
    other = IssueFactory.create()
    ReportFactory.create(issue=issue)
    ReportFactory.create_batch(4, issue=other)
    ConfirmationFactory.create(issue=other)

    annotated = _annotated(issue)

    assert annotated.report_total == 1  # type: ignore[attr-defined]
    assert annotated.corroboration_total == 1  # type: ignore[attr-defined]


def test_a_report_not_yet_attached_to_any_issue_is_counted_nowhere() -> None:
    """The state `POST /reports` leaves behind (`issue IS NULL`, BR-9). A subquery matching it would
    add every un-triaged report in the city to every Issue's count."""
    issue = IssueFactory.create()
    ReportFactory.create()  # unclustered

    annotated = _annotated(issue)

    assert annotated.report_total == 0  # type: ignore[attr-defined]
    assert annotated.corroboration_total == 0  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Severity: the displayed band, and the rank that orders it
# --------------------------------------------------------------------------------------


def test_the_band_annotation_matches_the_current_severity_property() -> None:
    issue = IssueFactory.create(computed_severity=SeveritySignal.HIGH)

    annotated = _annotated(issue)

    assert annotated.current_severity_band == issue.current_severity  # type: ignore[attr-defined]
    assert annotated.current_severity_band == SeveritySignal.HIGH  # type: ignore[attr-defined]


def test_an_override_wins_over_the_computed_band_in_sql_too() -> None:
    """⚠️ **`Coalesce(overridden, computed)` mirrors `overridden_severity or computed_severity`.**
    An Authority who downgrades an Issue has to see it move in the queue; annotating
    `computed_severity` would leave it ranked where it was, and the override would look like it had
    not taken effect (§6.5's `severity.current` is this value, and `?severity=` filters on it)."""
    issue = IssueFactory.create(
        computed_severity=SeveritySignal.CRITICAL,
        overridden_severity=SeveritySignal.LOW,
    )

    annotated = _annotated(issue)

    assert annotated.current_severity_band == SeveritySignal.LOW  # type: ignore[attr-defined]
    assert annotated.severity_rank == SEVERITY_RANK[SeveritySignal.LOW]  # type: ignore[attr-defined]
    assert annotated.current_severity_band == issue.current_severity  # type: ignore[attr-defined]


@pytest.mark.parametrize("band", list(SeveritySignal.values))
def test_every_band_ranks_off_the_one_shared_mapping(band: str) -> None:
    """⚠️ **`SEVERITY_RANK` is the source of the `When` clauses, never a second hand-written
    mapping.** Two mappings would agree until a band was added to one, and BR-11's "highest" is the
    only thing that constant sanctions being used for — ordering, and nothing more.

    The rank is a sort key and is never serialized (FR-21, C-10): `test_list.py` asserts its absence
    from the response body.
    """
    issue = IssueFactory.create(computed_severity=band)

    assert _annotated(issue).severity_rank == SEVERITY_RANK[band]  # type: ignore[attr-defined]


def test_the_four_bands_rank_strictly_critical_down_to_low() -> None:
    """The default sort's whole purpose (FR-22). Asserted as an ordering rather than four numbers so
    a re-scaling that preserves the order stays passing and an inversion cannot."""
    ranks = [
        _annotated(IssueFactory.create(computed_severity=band)).severity_rank  # type: ignore[attr-defined]
        for band in (
            SeveritySignal.CRITICAL,
            SeveritySignal.HIGH,
            SeveritySignal.MEDIUM,
            SeveritySignal.LOW,
        )
    ]

    assert ranks == sorted(ranks, reverse=True)
    assert len(set(ranks)) == 4


def test_an_unrecognized_band_sorts_last_instead_of_raising() -> None:
    """⚠️ **`default=Value(0)`, and it is a choice about blast radius.** A `KeyError` — or a `NULL`
    that `ORDER BY` parks at one end — would take out the entire queue page for one malformed row.
    The row is still worth showing; it just shows last.

    The band is written with an `UPDATE` because the model's `choices` would refuse it: this is the
    "data written by an older/newer deploy" case, not something an API caller can reach.
    """
    issue = IssueFactory.create()
    Issue.objects.filter(pk=issue.pk).update(computed_severity="catastrophic")

    annotated = _annotated(issue)

    assert annotated.severity_rank == 0  # type: ignore[attr-defined]
    assert min(SEVERITY_RANK.values()) > 0


# --------------------------------------------------------------------------------------
# The alias-collision trap
# --------------------------------------------------------------------------------------


def test_no_annotation_alias_shadows_a_model_property() -> None:
    """⚠️ **The reason the aliases are `_band` / `_total` rather than the obvious names.**

    Django assigns every annotation onto the model instance with `setattr`, and `current_severity`,
    `report_count` and `corroboration_count` are properties with no setter — so an annotation named
    after one of them raises `AttributeError` on the *first row fetched*, i.e. a `500` on the queue
    for a queryset that compiles perfectly. Asserted by name as well as by the fetches above, so the
    rule survives someone adding a fourth annotation.
    """
    queryset = annotate_queue_fields(Issue.objects.all())
    properties = {"current_severity", "report_count", "corroboration_count"}

    assert set(queryset.query.annotations) & properties == set()
    # And the properties still resolve on an annotated instance, which is the failure mode itself.
    IssueFactory.create()
    row = queryset.get()
    assert row.current_severity and row.report_count == 0
