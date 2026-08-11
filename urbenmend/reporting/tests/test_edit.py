"""`PATCH /reports/{id}` — the pre-triage edit and the human re-categorization (T2.8, API §6.3).

FR-11 grants two different rights to two different callers through one endpoint, and almost every
way of getting this wrong is silent:

1. **The author's window closes at triage; an official's never opens on `description`.** Collapsing
   the two into "may this user edit this report?" either lets an official rewrite a citizen's account
   of what they saw, or blocks the correction path FR-11 exists for.
2. **A human correction must not stamp `classified_at`.** Doing so looks like an improvement — the
   worker can no longer revert the official's decision — and produces a report that reads as triaged
   with a `NULL` severity band, which raises inside BR-11's `max()` in T4.6.
3. **BR-3 has to be re-checked on the way out.** The rule is "a photo *or* an adequate description";
   an edit is the one path that can leave a report with neither.

[doc: API §6.3; FR-11, FR-31, BR-3, BR-26; data-model "Ownership & Permissions"]
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from urbenmend.api.exceptions import Conflict
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.identity.models import User
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.media.models import MediaState
from urbenmend.media.tests.factories import ReadyMediaFactory
from urbenmend.reporting import services
from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus, SeveritySignal
from urbenmend.reporting.tests.factories import ClassifiedReportFactory, ReportFactory

# ⚠️ Imported from the detail suite rather than restated. §6.3 describes **one** Report resource and
# `ReportDetailSerializer` renders it for the read, the list items and this `PATCH` response — a
# second copy of the key set here could pass while the two endpoints disagreed.
from urbenmend.reporting.tests.test_detail import EXPECTED_KEYS

pytestmark = pytest.mark.django_db


def _url(report_id: object) -> str:
    return reverse("api:reports-detail", kwargs={"report_id": str(report_id)})


def _signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _patch(client: Client, report: Report, **body: Any) -> Any:
    """`PATCH` with a JSON body — `content_type` is explicit because the test client's default is not.

    Django's `Client.patch()` defaults to `application/octet-stream`, which DRF answers `415` to. The
    endpoint accepts JSON only (api-conventions.md), so every call here says so.
    """
    return client.patch(_url(report.pk), body, content_type="application/json")


def _scoped_authority(slug: str = "roads") -> User:
    """An active Authority holding exactly one category.

    Scope is granted through the M2M directly rather than through `set_category_scope()`: that
    service is the subject of the T1.6 suite, and routing a fixture through it would make one
    provisioning bug fail this module for an unrelated reason.
    """
    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug=slug))
    return authority


# --------------------------------------------------------------------------------------
# The author's window
# --------------------------------------------------------------------------------------


def test_the_author_may_correct_the_description_before_triage() -> None:
    report = ReportFactory.create()

    response = _patch(
        _signed_in(report.author), report, description="Actually the pothole is on the far kerb."
    )

    assert response.status_code == 200
    assert response.json()["description"] == "Actually the pothole is on the far kerb."
    report.refresh_from_db()
    assert report.description == "Actually the pothole is on the far kerb."


def test_the_response_is_the_report_resource() -> None:
    """§6.3 answers `200` with the resource, not `204` and not a bare `{"ok": true}`.

    A client that just edited needs the current row — `updatedAt` aside, an official's
    re-categorization changes `classification.source`, which the caller cannot compute itself.
    """
    report = ReportFactory.create()

    body = _patch(
        _signed_in(report.author), report, description="A longer description here."
    ).json()

    assert set(body) == EXPECTED_KEYS


def test_the_author_may_change_the_category() -> None:
    """FR-11's "citizens may override category". The source records who chose it."""
    report = ReportFactory.create()

    body = _patch(_signed_in(report.author), report, category="water_drainage").json()

    assert body["classification"]["category"] == "water_drainage"
    assert body["classification"]["source"] == ClassificationSource.CITIZEN


def test_a_category_only_edit_leaves_the_description_alone() -> None:
    """⚠️ **The trap is a default on the view's `.get()`.** `data.get("description", "")` reads a
    perfectly ordinary category-only edit as a request to blank the description — and on a photo-less
    report BR-3 would then reject it, so the visible symptom is a `400` on a field the client never
    sent. `None` has to mean "not sent" all the way down."""
    report = ReportFactory.create(description="The drain at the corner is blocked again.")

    body = _patch(_signed_in(report.author), report, category="water_drainage").json()

    assert body["description"] == "The drain at the corner is blocked again."


def test_the_author_may_still_edit_while_the_report_is_processing() -> None:
    """`is_editable` is `{submitted, processing}` — the window closes at *triage*, not at enqueue.

    A report sits in `processing` for as long as the worker queue is deep, and a citizen who spots
    their own typo one second after submitting must not be told it is too late.
    """
    report = ReportFactory.create(status=ReportStatus.PROCESSING)

    response = _patch(_signed_in(report.author), report, description="Corrected within the window.")

    assert response.status_code == 200


def test_the_author_may_not_edit_after_triage() -> None:
    """⚠️ §6.3's own `NOT_EDITABLE`, not the generic `CONFLICT`.

    A triaged report may already be clustered into an Issue an Authority is working, so rewriting the
    text underneath them would change what was triaged without re-triaging it. The specific code is
    what lets a client stop offering the edit affordance; `CONFLICT` is indistinguishable from a
    duplicate submission.
    """
    report = ReportFactory.create(status=ReportStatus.TRIAGED)

    response = _patch(_signed_in(report.author), report, description="Too late to change this.")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_EDITABLE"


def test_another_citizen_may_not_edit_someone_elses_report() -> None:
    """⚠️ `403`, and the message names neither the role nor the resource (T1.5).

    `404` would be the usual answer for a resource a caller may not see — but `GET /reports/{id}` is
    public, so this report's existence is not a secret this endpoint could keep, and a `404` would be
    a lie the same client disproves with one `GET`.
    """
    report = ReportFactory.create()

    response = _patch(_signed_in(UserFactory.create()), report, description="Not mine to edit.")

    assert response.status_code == 403
    message = response.json()["error"]["message"].lower()
    assert "citizen" not in message
    assert "author" not in message


def test_an_anonymous_patch_is_401_not_403() -> None:
    """⚠️ `AllowAny` is on this view for the public `GET`, so `request.user` can be `AnonymousUser`
    here. Without the explicit guard in `patch()` the service would read `.role` off a model that has
    none — a `500` on a request that should be a plain `401` (§4.2)."""
    report = ReportFactory.create()

    response = _patch(Client(), report, description="Nobody is signed in for this.")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


# --------------------------------------------------------------------------------------
# The official's re-categorization (FR-11)
# --------------------------------------------------------------------------------------


def test_an_authority_in_scope_may_re_categorize_after_triage() -> None:
    """⚠️ **No `is_editable` check on this path, and that is the point of FR-11.** The correction path
    exists because the LLM gets some wrong, and it gets them wrong *by classifying them* — so a rule
    that closed at triage would close before the mistake was visible."""
    report = ClassifiedReportFactory.create()

    body = _patch(_signed_in(_scoped_authority("roads")), report, category="water_drainage").json()

    assert body["classification"]["category"] == "water_drainage"
    assert body["classification"]["source"] == ClassificationSource.AUTHORITY


def test_an_admin_re_categorization_is_recorded_as_authority() -> None:
    """⚠️ **`ClassificationSource` has no `ADMIN` member, deliberately.**

    The column answers "who decided this category" at the granularity the pipeline cares about: the
    LLM, its fallback, the reporting citizen, or a human official. PRD §4.2 gives Admin every
    Authority capability, so an Admin re-categorizing is doing the Authority's job. A fifth member
    would split the "a human official set this" set in two, and the T3.5 query that must not
    overwrite a human decision would then need updating — and would fail open if it were not.
    """
    report = ClassifiedReportFactory.create()

    body = _patch(_signed_in(AdminFactory.create()), report, category="electrical").json()

    assert body["classification"]["source"] == ClassificationSource.AUTHORITY


def test_an_admin_bypasses_category_scope() -> None:
    """`has_category_scope()` returns true for an Admin without consulting any rows — seeding a row
    per category would silently un-scope them the moment a migration adds a node (T1.5)."""
    report = ClassifiedReportFactory.create()

    assert (
        _patch(_signed_in(AdminFactory.create()), report, category="sanitation_waste").status_code
        == 200
    )


def test_an_authority_outside_the_reports_scope_is_403() -> None:
    """BR-26. The report is in `roads`; this Authority holds only `water_drainage`."""
    report = ClassifiedReportFactory.create()

    response = _patch(
        _signed_in(_scoped_authority("water_drainage")), report, category="electrical"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    report.refresh_from_db()
    assert report.category is not None
    assert report.category.slug == "roads"


def test_an_authority_may_move_a_report_out_of_their_own_scope() -> None:
    """⚠️ **Only the report's *current* category must be in scope, never the target — and tightening
    this looks like a security fix.**

    Requiring the destination in scope would make cross-department re-routing impossible, which is
    the primary thing re-categorization is for ("this isn't roads, it's water"). The consequence is
    real and intended: the report leaves this Authority's own queue the moment they file it correctly.
    """
    report = ClassifiedReportFactory.create()
    authority = _scoped_authority("roads")

    response = _patch(_signed_in(authority), report, category="water_drainage")

    assert response.status_code == 200
    assert not authority.category_scope.filter(slug="water_drainage").exists()


def test_an_authority_may_not_edit_the_description() -> None:
    """⚠️ The description is the citizen's own account of what they saw. An official editing it is not
    a correction, it is a rewrite of evidence — and §6.3 grants Authority `U⚙️ (re-categorize)`, not a
    general update (data-model "Ownership & Permissions")."""
    report = ClassifiedReportFactory.create()

    response = _patch(
        _signed_in(_scoped_authority("roads")), report, description="Rewritten by an official."
    )

    assert response.status_code == 403
    report.refresh_from_db()
    assert "Rewritten" not in report.description


def test_an_authority_may_not_send_a_description_alongside_a_category() -> None:
    """The refusal is on the *field*, not on the request shape — so bundling it with a legitimate
    re-categorization does not smuggle it through, and the category change is refused with it."""
    report = ClassifiedReportFactory.create()

    response = _patch(
        _signed_in(_scoped_authority("roads")),
        report,
        category="water_drainage",
        description="Rewritten by an official.",
    )

    assert response.status_code == 403
    report.refresh_from_db()
    assert report.category is not None
    assert report.category.slug == "roads"


def test_an_authority_may_not_re_categorize_an_unclassified_report() -> None:
    """⚠️ An unclassified report has no category, so no Authority is in scope for it — the same rule
    `list_reports()` applies, where a report nobody has categorized appears in no Authority's queue.
    Clearing that backlog is T3.5's job, not an Authority's to claim."""
    report = ReportFactory.create()

    response = _patch(_signed_in(_scoped_authority("roads")), report, category="roads")

    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# What a human correction must NOT touch
# --------------------------------------------------------------------------------------


def test_a_human_re_categorization_leaves_the_triage_fields_alone() -> None:
    """⚠️ **The severity signal, the confidence and `classified_at` all survive the correction.**

    Severity lives on the **Issue**, never on the Report, so FR-11's "re-severity" is §6.5's override
    and not this endpoint's — an Authority who wants a different severity is not served by silently
    clearing the machine's signal here. Clearing `confidence` would likewise destroy the evaluation
    signal FR-11's second half ("can seed prompt examples / evaluation sets") depends on.
    """
    report = ClassifiedReportFactory.create()
    classified_at = report.classified_at

    _patch(_signed_in(AdminFactory.create()), report, category="water_drainage")

    report.refresh_from_db()
    assert report.classified_at == classified_at
    assert report.severity_signal == SeveritySignal.MEDIUM
    assert report.confidence == 0.82


def test_a_citizen_correction_does_not_mark_the_report_classified() -> None:
    """⚠️ **The trap: stamping `classified_at` here would look like an improvement.**

    It would make `is_classified` true, so T3.5's worker skips the report and cannot revert the
    human's category. What it actually produces is a report that *reads* as triaged while
    `severity_signal` is still `NULL` — and `SEVERITY_RANK[None]` raises inside BR-11's `max()` when
    T4.6 computes the Issue's severity. The protection T3.5 must implement is on
    `classification_source` instead: it must not overwrite a category whose source is `authority`.
    """
    report = ReportFactory.create()

    _patch(_signed_in(report.author), report, category="water_drainage")

    report.refresh_from_db()
    assert report.classified_at is None
    assert report.is_classified is False
    assert report.severity_signal is None


def test_the_edit_bumps_updated_at() -> None:
    """⚠️ `updated_at` is `auto_now`, and `save(update_fields=[...])` writes only the named columns —
    so omitting it from the list leaves an edited row reading as never-modified. The same trap
    `submit_report()`'s status flip records."""
    report = ReportFactory.create()
    before = report.updated_at

    _patch(_signed_in(report.author), report, description="Edited, so the timestamp must move.")

    report.refresh_from_db()
    assert report.updated_at > before


# --------------------------------------------------------------------------------------
# The body: what is refused rather than dropped
# --------------------------------------------------------------------------------------


def test_an_empty_body_is_400() -> None:
    """⚠️ A `200` here is a silent success: a client whose field name is wrong (and therefore stripped
    by its own serialization) would see every edit "succeed" and nothing change."""
    report = ReportFactory.create()

    response = _patch(_signed_in(report.author), report)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_a_severity_in_the_body_is_refused_rather_than_dropped() -> None:
    """⚠️ **FR-11 says authorities may "re-severity", and this endpoint is still not where.** Severity
    lives on the Issue (§6.5's Authority override), so DRF's default — drop the unknown key, answer
    `200` — would leave an Authority believing they had just escalated a Critical report."""
    report = ClassifiedReportFactory.create()

    response = _patch(_signed_in(_scoped_authority("roads")), report, severity="critical")

    assert response.status_code == 400
    assert [detail["field"] for detail in response.json()["error"]["details"]] == ["severity"]


def test_a_status_change_is_refused() -> None:
    """The same rule catching the other adjacent derived column — a client must not be able to mark
    its own report `triaged` and skip the pipeline."""
    report = ReportFactory.create()

    response = _patch(_signed_in(report.author), report, status=ReportStatus.TRIAGED)

    assert response.status_code == 400
    report.refresh_from_db()
    assert report.status == ReportStatus.SUBMITTED


def test_a_null_category_is_refused() -> None:
    """⚠️ Clearing a category is not an edit — it is a request to re-run triage, which this endpoint
    does not do, and it would leave `classification_source` naming a human with no decision attached.
    `null` is also what makes "not sent" unambiguous inside `update_report()`."""
    report = ClassifiedReportFactory.create()

    response = _patch(_signed_in(AdminFactory.create()), report, category=None)

    assert response.status_code == 400
    report.refresh_from_db()
    assert report.category is not None


def test_an_unknown_category_is_400() -> None:
    report = ReportFactory.create()

    response = _patch(_signed_in(report.author), report, category="teleportation_pads")

    assert response.status_code == 400


def test_a_retired_category_is_refused() -> None:
    """⚠️ Refused, not coerced to `Other`. BR-7's coercion is for an *LLM* returning something
    off-taxonomy — a machine's unusable answer. Filing a human's explicit correction under
    `Other / Uncategorized` would lose the decision they just made. Note this differs from
    `?category=` on the list, where a retired slug stays filterable so old reports remain findable.
    """
    retired = Category.objects.get(slug="sanitation_waste")
    retired.status = CategoryStatus.RETIRED
    retired.save(update_fields=["status"])
    report = ReportFactory.create()

    response = _patch(_signed_in(report.author), report, category="sanitation_waste")

    assert response.status_code == 400


# --------------------------------------------------------------------------------------
# BR-3, re-checked on the way out
# --------------------------------------------------------------------------------------


def test_a_short_description_is_refused_on_a_photo_less_report() -> None:
    """BR-3 is not an intake-only rule. Editing is the one path that can leave a report with neither
    a photo nor an adequate description — a state `POST /reports` cannot produce."""
    report = ReportFactory.create()

    response = _patch(_signed_in(report.author), report, description="oops")

    assert response.status_code == 400
    report.refresh_from_db()
    assert report.description != "oops"


def test_the_description_may_be_blanked_when_a_photo_carries_the_report() -> None:
    """BR-3 accepts either. A citizen who realizes their text added nothing to the photo may remove
    it, and refusing that would be inventing a rule intake does not have."""
    report = ReportFactory.create()
    ReadyMediaFactory.create(report=report, owner=report.author)

    response = _patch(_signed_in(report.author), report, description="")

    assert response.status_code == 200
    report.refresh_from_db()
    assert report.description == ""


def test_blanking_the_description_is_refused_when_the_only_photo_was_removed() -> None:
    """⚠️ **`visible_media_count()`, not `report.media.count()` — the moderated row must not count as
    evidence.** FR-31 retains the row (no hard deletes), so a naive count sees a photo that no client
    can see, and the report ends up with no content at all while every assertion about it passes."""
    report = ReportFactory.create()
    ReadyMediaFactory.create(report=report, owner=report.author, state=MediaState.REMOVED)

    response = _patch(_signed_in(report.author), report, description="")

    assert response.status_code == 400


# --------------------------------------------------------------------------------------
# Absence and moderation
# --------------------------------------------------------------------------------------


def test_an_unknown_id_is_404() -> None:
    citizen = UserFactory.create()
    client = _signed_in(citizen)

    response = client.patch(
        _url(uuid.uuid4()), {"description": "No such report to edit."}, "application/json"
    )

    assert response.status_code == 404


@pytest.mark.parametrize("status", [ReportStatus.HIDDEN, ReportStatus.REMOVED])
def test_a_moderated_report_is_410_to_its_own_author(status: str) -> None:
    """⚠️ Both verbs route through `get_report_for_read()`, so the `PATCH` inherits §6.13's `410`
    rather than answering `409` or a `403` that would confirm the row is still there. The author is
    told the content is gone — the same answer `GET` gives them — and the review surface is §6.13's."""
    report = ReportFactory.create(status=status)

    response = _patch(_signed_in(report.author), report, description="Trying to edit a hidden row.")

    assert response.status_code == 410


def test_the_service_refuses_a_moderated_report_directly() -> None:
    """⚠️ **Unreachable from the endpoint, and kept anyway.** The view's selector raises `410` first,
    but a management command holding a `Report` does not go through it — and without this guard an
    Authority could re-categorize suppressed content back into a live queue, undoing FR-31 through a
    code path nobody was looking at."""
    report = ClassifiedReportFactory.create(status=ReportStatus.HIDDEN)

    with pytest.raises(Conflict):
        services.update_report(
            actor=AdminFactory.create(), report=report, category_slug="water_drainage"
        )
