"""
P4 clustering-concurrency test — R-2's acceptance criterion, written in P0 (A10, T4.4).

✅ T4.4 now implements the service this acceptance test pins. The test remains focused on the
race rather than duplicating the find-or-create implementation inline.
[doc: Plan §8.1, P0 checkpoint]: R-2 ("duplicate Issues under concurrent submission", Plan §risks)
is the single most expensive defect to discover late, so the acceptance criteria are locked into
the suite *before* the schedule pressure that would otherwise trim them.

It encodes the Arch §4.3 contract and nothing more:

    the find-or-create runs inside a single DB transaction guarded by a lock keyed on a
    coarse spatial+category bucket (e.g. a geohash cell + category) ... 1. ST_DWithin query
    for an **open** Issue of the same category within the configured radius and time window.
    2. If found -> attach; if not -> create new Issue.

✅ The test calls `cluster_report()` rather than inlining a find-or-create.
An inlined version would only ever test itself, and would still pass in P4 while the real
service raced. The seam asserted here is the one T4.4 must implement.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from django.contrib.gis.geos import Point
from django.db import close_old_connections, connection

if TYPE_CHECKING:
    from urbenmend.classification.models import Category
    from urbenmend.identity.models import User
    from urbenmend.reporting.models import Report

# Dhaka. Both reports carry the *same* coordinate, so they are inside any conservative radius
# a category could be configured with — the test cannot become order- or tuning-dependent.
# ⚠️ Deliberately not asserting a radius: radius and time-window are per-category reference
# data (ASSUMP-4, NFR-11, Arch §4.3) and hard-coding 50m here would contradict that.
LOCATION = Point(90.4125, 23.8103, srid=4326)

# Threads, not processes: `transaction=True` gives each thread a real connection and a real
# transaction, which is what makes the advisory lock observable. The race is the one the real
# path produces — two Celery workers each running the triage job for a different Report
# (enqueued via `transaction.on_commit`, Arch §4.1) against the same real-world issue.
CONCURRENT_SUBMISSIONS = 2


def _cluster_in_thread(report_id: uuid.UUID) -> uuid.UUID:
    """Run with a thread-local connection and close it before pytest drops the test database."""
    from urbenmend.issues.services import cluster_report

    close_old_connections()
    try:
        return cluster_report(report_id)
    finally:
        connection.close()


@pytest.mark.django_db(transaction=True)
def test_concurrent_reports_of_one_real_world_issue_create_exactly_one_issue() -> None:
    """Two same-category reports at one location, submitted in parallel -> exactly one Issue.

    This is R-2's mitigation test and the concurrency half of the M4 DoD ("two nearby
    same-category reports cluster into one Issue without creating duplicates under concurrent
    submission", Plan §P4).

    Without the lock, both workers run `SELECT ... ST_DWithin`, both see no open Issue, and both
    `INSERT` — a lost-update race. No error surfaces; the damage is two Issues for one pothole,
    which splits the report count authorities triage on and double-notifies subscribers.
    """
    from urbenmend.issues.models import Issue

    category = _seed_category()
    reports = [
        _seed_report(category=category, location=LOCATION, submitter=_seed_citizen(index))
        for index in range(CONCURRENT_SUBMISSIONS)
    ]

    # Fire the triage jobs simultaneously. `max_workers` matches the submission count so the
    # calls genuinely overlap rather than queueing behind one another.
    with ThreadPoolExecutor(max_workers=CONCURRENT_SUBMISSIONS) as executor:
        issue_ids: list[uuid.UUID] = [
            future.result()
            for future in [executor.submit(_cluster_in_thread, report.id) for report in reports]
        ]

    # Both reports must have landed on the same Issue...
    assert len(set(issue_ids)) == 1, (
        f"R-2: concurrent submissions produced {len(set(issue_ids))} Issues ({issue_ids}) "
        f"instead of clustering into one. The find-or-create is not race-safe — check that "
        f"the advisory lock (geohash cell + category) is taken inside the atomic block."
    )

    # ...and the database must agree. Asserting only the return values would miss an
    # implementation that created two rows and happened to return the same id from both.
    assert Issue.objects.count() == 1

    # Every Report must be attached. A lock that serialises correctly but drops the second
    # report's attachment would satisfy "one Issue" while losing a citizen's submission.
    issue = Issue.objects.get()
    assert issue.reports.count() == CONCURRENT_SUBMISSIONS


# ------------------------------------------------------------------------------------------
# Fixtures.
#
# ✅ **T2.1 replaced the hand-rolled `_seed_*` helpers with `factory_boy` factories**, which is
# what the P0 note here anticipated ("kept as thin helpers so that when the models land, the diff
# is confined to these three functions and the assertions above stand unchanged" — it is, and
# they do). The original forcing function was `xfail(strict=True)` plus mypy's
# `warn_unused_ignores`; T4.4 removed both once the service became real.
#
# ✅ T4.4 removed the strict xfail and the final self-cleaning import ignore.
# ------------------------------------------------------------------------------------------
def _seed_citizen(index: int) -> User:
    """One citizen account. Distinct submitters, because two reports from one account at one
    point is the duplicate-submission case (BR-23), not the clustering case.

    `index` is kept in the signature although `UserFactory` sequences emails on its own — the
    call site reads as "citizen 0 and citizen 1", and dropping it would make the distinctness
    the assertions depend on an invisible property of the factory.
    """
    from urbenmend.identity.tests.factories import UserFactory

    return UserFactory.create(email=f"citizen-{index}@example.test")


def _seed_category() -> Category:
    """A category from the controlled taxonomy (C-2, no free-form categories).

    ✅ Q1 is resolved and `classification/0001` seeds the seven real nodes, so this now returns
    a **real** one rather than inventing `test-category`. Clustering keys on category, and a node
    no report will ever carry would make the match trivially true.

    ⚠️ The clustering radius is deliberately still not asserted here: T4.3 stores radius and time
    window as per-category reference data, and T4.4 must resolve that rule rather than hard-code a
    value into this concurrency test.
    """
    from urbenmend.classification.models import Category

    return Category.objects.get(slug="roads")


def _seed_report(*, category: Category, location: Point, submitter: User) -> Report:
    """A classified Report, ready to cluster.

    ⚠️ Pre-classified on purpose. Clustering matches on category, and category is produced by
    classification, so classification must complete first *within the same triage job*
    (Arch §4.2). This test isolates the clustering race; it is not a triage-pipeline test.

    ⚠️ Built with `ReportFactory`, not `create_report()`. The service enforces BR-35 against the
    seeded city boundary, and this test's fixed `LOCATION` is a clustering coordinate, not an
    intake case — routing through intake would make a boundary change break a concurrency test.
    """
    from urbenmend.reporting.tests.factories import ClassifiedReportFactory

    return ClassifiedReportFactory.create(category=category, location=location, author=submitter)
