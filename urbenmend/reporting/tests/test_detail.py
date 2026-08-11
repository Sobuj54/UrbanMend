"""`GET /reports/{id}` — the public read (T2.7, API §6.3).

Two rules carry this module. The read is **public** (Q7 resolved), so it must work with no session
at all; and moderation is a **state change** rather than a row delete, which is the only reason
`410 GONE` is expressible for a hidden report instead of a `404` that would look like a bug to the
citizen who filed it.

⚠️ **The body asserted here is the whole point of the task.** §6.3's Report resource nests
`location`, `classification` and `media[]`, and every one of those three is a place where a
plausible implementation is silently wrong: transposed coordinates, a `""` severity band that reads
as a fifth value, or a moderated photo still listed. Each has a test below rather than an assertion
folded into the happy path.

[doc: API §6.3, §4.2; FR-11, FR-31, Q7; database.md "No hard deletes"]
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from urbenmend.classification.models import Category
from urbenmend.media.models import MediaState
from urbenmend.media.tests.factories import MediaFactory, ReadyMediaFactory
from urbenmend.reporting.models import ClassificationSource, ReportStatus, SeveritySignal
from urbenmend.reporting.tests.factories import (
    DEFAULT_LOCATION,
    ClassifiedReportFactory,
    ReportFactory,
)

pytestmark = pytest.mark.django_db

# §6.3's Report resource, exactly. Asserted as a set rather than key-by-key so an *added* field
# fails too: every neighbouring column on the row is either derived data the conventions make
# read-only or operator-facing text (`classification_rationale`, `classification_model`), and a
# `ModelSerializer` slip that published them would pass a key-by-key check unchanged.
EXPECTED_KEYS = {
    "id",
    "authorId",
    "description",
    "location",
    "media",
    "classification",
    "issueId",
    "status",
    "createdAt",
}


def _url(report_id: object) -> str:
    return reverse("api:reports-detail", kwargs={"report_id": str(report_id)})


# --------------------------------------------------------------------------------------
# The public read
# --------------------------------------------------------------------------------------


def test_an_anonymous_caller_may_read_a_report() -> None:
    """Q7 RESOLVED: report visibility is public.

    ⚠️ This is why `ReportDetailView` overrides the project-wide `IsAuthenticated` default with
    `AllowAny` — without it the endpoint answers `401` to the map and to every unauthenticated
    citizen checking a link they were sent.
    """
    report = ReportFactory.create()

    response = Client().get(_url(report.pk))

    assert response.status_code == 200
    assert response.json()["id"] == str(report.pk)


def test_the_body_carries_exactly_the_documented_fields() -> None:
    """See `EXPECTED_KEYS` — an added key fails this as loudly as a missing one."""
    report = ReportFactory.create()

    body = Client().get(_url(report.pk)).json()

    assert set(body) == EXPECTED_KEYS


def test_the_operator_facing_classification_text_never_crosses_the_wire() -> None:
    """⚠️ `classification_rationale` is FR-15's explanation, written for a human reviewing triage.

    §6.3's Report resource does not carry it, and it is LLM-authored free text derived from the
    citizen's description — so publishing it on a public endpoint would republish that description
    through a channel nobody reviewed (NFR-12). `classification_model` is an internal provider
    string (❓Q9) with the same problem.
    """
    report = ClassifiedReportFactory.create(
        classification_rationale="Sensitive operator note about the reporter.",
        classification_model="internal-provider/v9",
    )

    payload = Client().get(_url(report.pk)).content.decode()

    assert "Sensitive operator note" not in payload
    assert "internal-provider" not in payload


def test_created_at_is_iso_8601_utc() -> None:
    """§1.2 fixes `2026-07-22T10:15:30Z`. `DATETIME_FORMAT = "iso-8601"` is what produces it."""
    report = ReportFactory.create()

    created_at = Client().get(_url(report.pk)).json()["createdAt"]

    assert created_at.endswith("Z")


# --------------------------------------------------------------------------------------
# The nested location
# --------------------------------------------------------------------------------------


def test_location_is_nested_lng_lat_and_address() -> None:
    """⚠️ **`lng` and `lat` read off `.x` and `.y`, and the pairing is the trap.**

    A `Point` stores longitude in `x`; emitting `{"lng": location.y}` transposes every coordinate
    in the product. It would not raise, and Dhaka's own numbers (90.4, 23.8) are both valid
    coordinates on their own — so the only thing that catches it is asserting the two separately
    against known values, which is what this does.
    """
    report = ReportFactory.create(address="Mirpur Road, Dhanmondi")

    location = Client().get(_url(report.pk)).json()["location"]

    assert location == {
        "lng": DEFAULT_LOCATION.x,
        "lat": DEFAULT_LOCATION.y,
        "address": "Mirpur Road, Dhanmondi",
    }


# --------------------------------------------------------------------------------------
# The nested classification
# --------------------------------------------------------------------------------------


def test_classification_is_four_explicit_nulls_before_triage() -> None:
    """⚠️ **Present and null, never absent or `{}`.** BR-9 makes an unclassified Report valid, and
    §6.3 shows the block on every Report — so a client renders "triage pending" from the null, not
    from a `KeyError`. Omitting the block would make the pending state indistinguishable from an
    older server that never sent it."""
    report = ReportFactory.create()

    classification = Client().get(_url(report.pk)).json()["classification"]

    assert classification == {
        "category": None,
        "severitySignal": None,
        "confidence": None,
        "source": None,
    }


def test_a_citizen_hint_shows_as_a_category_while_severity_stays_null() -> None:
    """⚠️ **A hint is not a classification** (T2.1). `category` is filled from the citizen's
    optional slug, but `severitySignal`, `confidence` and `source` stay null until the worker runs —
    so a client must not infer "triaged" from a non-null category, and neither must T3.5's
    worker query."""
    report = ReportFactory.create(category=Category.objects.get(slug="roads"))

    classification = Client().get(_url(report.pk)).json()["classification"]

    assert classification["category"] == "roads"
    assert classification["severitySignal"] is None
    assert classification["source"] is None


def test_classification_is_populated_after_triage() -> None:
    """The slug is emitted, not the numeric FK — `slug` is the machine key (T0.10, §6.2)."""
    report = ClassifiedReportFactory.create()

    classification = Client().get(_url(report.pk)).json()["classification"]

    assert classification == {
        "category": "roads",
        "severitySignal": SeveritySignal.MEDIUM,
        "confidence": 0.82,
        "source": ClassificationSource.LLM,
    }


def test_an_empty_string_severity_band_is_emitted_as_null() -> None:
    """⚠️ **`or None` on the two nullable choice columns, and it is not cosmetic.**

    `severity_signal` and `classification_source` are `CharField(null=True)` carrying the `DJ001`
    exemption, so `""` is reachable by any code path that assigns a blank instead of `None` — and
    `""` is an undeclared fifth severity band that a client's `switch` would fall through and
    `SEVERITY_RANK[""]` would raise on inside BR-11's `max()`. It must never leave the API as a
    value.
    """
    report = ReportFactory.create(severity_signal="", classification_source="")

    classification = Client().get(_url(report.pk)).json()["classification"]

    assert classification["severitySignal"] is None
    assert classification["source"] is None


def test_issue_id_is_null_until_clustering_exists() -> None:
    """⚠️ Declared and null rather than omitted — the Issue FK lands in T4.1, and §6.3 shows the
    key on every Report. Whoever adds the FK replaces one method here; a client written today
    already handles the null."""
    report = ReportFactory.create()

    assert Client().get(_url(report.pk)).json()["issueId"] is None


# --------------------------------------------------------------------------------------
# The nested media list
# --------------------------------------------------------------------------------------


def test_media_carries_the_attached_photos_with_urls() -> None:
    report = ReportFactory.create()
    media = ReadyMediaFactory.create(report=report, owner=report.author)

    body = Client().get(_url(report.pk)).json()

    assert len(body["media"]) == 1
    entry = body["media"][0]
    assert entry["id"] == str(media.pk)
    assert entry["state"] == MediaState.READY
    assert entry["url"]
    assert entry["thumbnailUrl"]


def test_media_omits_a_photo_moderation_removed() -> None:
    """⚠️ FR-31: the row is retained (no hard deletes), so it is `state = removed` and still
    joined to the report. Filtering on the query — not on the serializer — is what keeps it out;
    an untouched `report.media.all()` would list it with two null URLs and a `removed` state,
    publishing the fact that something was taken down."""
    report = ReportFactory.create()
    kept = ReadyMediaFactory.create(report=report, owner=report.author)
    ReadyMediaFactory.create(report=report, owner=report.author, state=MediaState.REMOVED)

    body = Client().get(_url(report.pk)).json()

    assert [entry["id"] for entry in body["media"]] == [str(kept.pk)]


def test_media_is_ordered_oldest_first() -> None:
    """The citizen's own upload order. Unordered, PostgreSQL is free to return the rows in any
    order, so a two-photo report would show its "before" and "after" shots inconsistently between
    requests — a difference no assertion on a one-photo fixture can see."""
    report = ReportFactory.create()
    first = MediaFactory.create(report=report, owner=report.author)
    second = MediaFactory.create(report=report, owner=report.author)

    body = Client().get(_url(report.pk)).json()

    assert [entry["id"] for entry in body["media"]] == [str(first.pk), str(second.pk)]


def test_media_is_an_empty_list_for_a_description_only_report() -> None:
    """BR-3 accepts either a photo or a description, so this is an ordinary report, not a defect."""
    report = ReportFactory.create()

    assert Client().get(_url(report.pk)).json()["media"] == []


# --------------------------------------------------------------------------------------
# Absence, and the moderation split
# --------------------------------------------------------------------------------------


def test_an_unknown_id_is_404() -> None:
    response = Client().get(_url(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_non_uuid_id_is_404_from_the_router() -> None:
    """⚠️ `<uuid:report_id>` refuses it at the routing layer, so a scan for `/reports/1` never
    reaches a view — and no selector has to defend against a `ValueError` from the ORM to avoid a
    `500`."""
    response = Client().get("/api/v1/reports/1")

    assert response.status_code == 404


@pytest.mark.parametrize("status", [ReportStatus.HIDDEN, ReportStatus.REMOVED])
def test_a_moderated_report_is_410_not_404(status: str) -> None:
    """⚠️ **§6.13 fixes `410` for both moderation outcomes, and the distinction is deliberate.**

    `404` means "no such report", which for content that existed and was linked to is a lie the
    author would read as data loss. `410` says the content was taken down without saying by whom or
    why (FR-31). Both `hidden` and `removed` answer it: they differ in whether the bytes survive,
    which is an operator-facing distinction, not one the public read should expose.
    """
    report = ReportFactory.create(status=status)

    response = Client().get(_url(report.pk))

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "GONE"


def test_a_moderated_report_answers_410_to_its_own_author() -> None:
    """⚠️ No author exemption, on purpose. A moderated report readable by its author is a channel
    for the author to confirm exactly what a moderator did, and the author-facing explanation is a
    notification (P6/T6.x), not a `200` on the public read."""
    report = ReportFactory.create(status=ReportStatus.HIDDEN)
    client = Client()
    client.force_login(report.author)

    assert client.get(_url(report.pk)).status_code == 410
