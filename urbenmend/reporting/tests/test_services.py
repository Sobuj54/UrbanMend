"""T2.1 — `create_report()` and the validation primitives (FR-5, BR-1/2/3/35, C-11).

The service is the enforcement point (FR-3, Arch §3.1): `POST /reports` (T2.2) is a thin caller,
so every rule asserted here holds for a management command or a worker too.

[doc: testing.md "out-of-city rejection (C-11)"; API §6.3]
"""

from __future__ import annotations

import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied

from urbenmend.api.exceptions import OutOfCity
from urbenmend.classification.models import Category
from urbenmend.classification.tests.factories import RetiredCategoryFactory
from urbenmend.geo.models import CityBoundary
from urbenmend.identity.tests.factories import (
    AdminFactory,
    AuthorityFactory,
    RegisteredUserFactory,
    UserFactory,
)
from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus
from urbenmend.reporting.services import (
    MIN_DESCRIPTION_LENGTH,
    ReportValidationError,
    create_report,
    validate_location,
    validate_report_content,
)

pytestmark = pytest.mark.django_db

# Inside `docs/city-boundary/dhaka-demo.geojson`, which the migration seeds into every test DB.
IN_CITY = Point(90.4125, 23.8103, srid=4326)
# A real coordinate that is simply not in the served city — the BR-35 case. Deliberately not
# `(0, 0)`: a null-island control would pass an out-of-city test while proving far less.
OUT_OF_CITY_POINT = Point(88.3639, 22.5726, srid=4326)  # Kolkata
GOOD_DESCRIPTION = "Deep pothole in the middle of the northbound lane."


# ---------------------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------------------
def test_create_report_persists_a_submitted_unclassified_report() -> None:
    """The state `POST /reports` leaves behind: submitted, unclassified, located (BR-9)."""
    citizen = UserFactory.create()

    report = create_report(author=citizen, location=IN_CITY, description=GOOD_DESCRIPTION)

    assert report.pk is not None
    assert report.author == citizen
    assert report.status == ReportStatus.SUBMITTED
    assert report.category is None
    assert report.classification_source is None
    assert report.is_classified is False
    assert report.location.equals_exact(IN_CITY, tolerance=1e-9)


def test_create_report_strips_whitespace_from_text() -> None:
    """Otherwise a description of spaces passes BR-3's length check on the raw string."""
    report = create_report(
        author=UserFactory.create(),
        location=IN_CITY,
        description=f"  {GOOD_DESCRIPTION}  ",
        address="  12 Test Road  ",
    )

    assert report.description == GOOD_DESCRIPTION
    assert report.address == "12 Test Road"


def test_status_is_not_caller_supplied() -> None:
    """⚠️ Structural: `create_report()` takes no `status` argument, by signature.

    Asserted against the signature rather than by passing one, because the guarantee is that the
    parameter cannot exist — a caller marking a report `triaged` would skip the whole pipeline.
    """
    import inspect

    assert "status" not in inspect.signature(create_report).parameters


# ---------------------------------------------------------------------------------------
# BR-1 / authorization — Citizen only (API §6.3)
# ---------------------------------------------------------------------------------------
def test_an_authority_may_not_submit_a_report() -> None:
    """The ownership matrix grants Authority `R⚙️/U⚙️` on Report, not `C` (data-model)."""
    with pytest.raises(PermissionDenied):
        create_report(
            author=AuthorityFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION
        )


def test_an_admin_may_not_submit_a_report() -> None:
    with pytest.raises(PermissionDenied):
        create_report(author=AdminFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION)


def test_a_refused_role_creates_no_row() -> None:
    """⚠️ The check runs before any write, so a `403` leaves nothing behind.

    Without this, reordering the authorization check below `Report.objects.create()` would still
    raise and still pass the two tests above, while persisting the report it rejected.
    """
    with pytest.raises(PermissionDenied):
        create_report(
            author=AuthorityFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION
        )

    assert Report.objects.count() == 0


def test_a_registered_unverified_citizen_may_submit() -> None:
    """⚠️ Deliberate: BR-30 gates *notification* on verification, not submission.

    The limited capability set for unverified accounts is explicitly unspecified (auth.md:
    "don't invent it"), so intake is not narrowed here. A citizen who has just registered can
    still report a live hazard.
    """
    report = create_report(
        author=RegisteredUserFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION
    )

    assert report.pk is not None


# ---------------------------------------------------------------------------------------
# BR-2 / BR-35 — location (C-3, C-11)
# ---------------------------------------------------------------------------------------
def test_a_report_without_a_location_is_rejected() -> None:
    """BR-2 — a Report cannot be submitted without a location."""
    with pytest.raises(ReportValidationError):
        create_report(author=UserFactory.create(), location=None, description=GOOD_DESCRIPTION)


def test_an_out_of_city_location_raises_out_of_city_not_a_generic_error() -> None:
    """⚠️ C-11's `422 OUT_OF_CITY`, and the *code* is the point.

    A well-formed coordinate outside the city is a business-rule violation, not a malformed
    body: API §6.3 names `OUT_OF_CITY` explicitly, and collapsing it into the generic
    `VALIDATION_FAILED` would leave a client unable to tell "fix your JSON" from "we do not
    serve your city".
    """
    with pytest.raises(OutOfCity) as excinfo:
        create_report(
            author=UserFactory.create(), location=OUT_OF_CITY_POINT, description=GOOD_DESCRIPTION
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.get_codes() == "OUT_OF_CITY"


def test_out_of_city_and_missing_location_raise_different_types() -> None:
    """`400 VALIDATION_FAILED` vs `422 OUT_OF_CITY` — the distinction API §6.3 draws."""
    with pytest.raises(ReportValidationError):
        validate_location(location=None)

    with pytest.raises(OutOfCity):
        validate_location(location=OUT_OF_CITY_POINT)


def test_an_out_of_city_submission_creates_no_row() -> None:
    with pytest.raises(OutOfCity):
        create_report(
            author=UserFactory.create(), location=OUT_OF_CITY_POINT, description=GOOD_DESCRIPTION
        )

    assert Report.objects.count() == 0


def test_intake_fails_closed_when_no_boundary_is_configured() -> None:
    """⚠️ **The fail-closed decision, asserted.**

    Arch §409 sanctions skipping the out-of-city check when the boundary dependency is missing.
    T2.1 does not take that degradation: C-11 says such a location "is not accepted", and a
    silently-disabled constraint leaves no trace. An empty boundary table rejects everything
    loudly instead — visible in minutes, rather than discovered months later in the data.
    """
    CityBoundary.objects.update(is_active=False)

    with pytest.raises(OutOfCity):
        create_report(author=UserFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION)


def test_location_is_stored_in_4326() -> None:
    """C-3 — the explicit report coordinate, in the SRID the boundary check compares against."""
    report = create_report(
        author=UserFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION
    )
    report.refresh_from_db()

    assert report.location.srid == 4326


# ---------------------------------------------------------------------------------------
# BR-3 — at least one of {photo, adequate description}
# ---------------------------------------------------------------------------------------
def test_a_report_with_neither_photo_nor_description_is_rejected() -> None:
    with pytest.raises(ReportValidationError):
        create_report(author=UserFactory.create(), location=IN_CITY, description="", media_count=0)


def test_a_too_short_description_with_no_photo_is_rejected() -> None:
    with pytest.raises(ReportValidationError):
        create_report(
            author=UserFactory.create(), location=IN_CITY, description="pothole", media_count=0
        )


def test_a_whitespace_only_description_is_rejected() -> None:
    """⚠️ The check strips first — otherwise 15 spaces satisfies BR-3."""
    with pytest.raises(ReportValidationError):
        validate_report_content(description=" " * (MIN_DESCRIPTION_LENGTH + 5), media_count=0)


def test_a_photo_exempts_a_report_from_the_description_rule() -> None:
    """BR-3 is "at least one of" — a photo-only submission is valid (FR-5)."""
    report = create_report(
        author=UserFactory.create(), location=IN_CITY, description="", media_count=1
    )

    assert report.description == ""


def test_a_photo_exempts_even_an_empty_description() -> None:
    """The `media_count > 0` branch returns before the length check runs at all."""
    validate_report_content(description="", media_count=2)


def test_a_bangla_description_at_the_threshold_is_accepted() -> None:
    """NFR-8 — the threshold counts characters, not bytes.

    ⚠️ Bangla says more per character than English, so a byte-based or word-based rule would
    reject legitimate reports in the city's own language while accepting thinner English ones.
    """
    bangla = "রাস্তায় বড় গর্ত রয়েছে এখানে"

    assert len(bangla) >= MIN_DESCRIPTION_LENGTH
    validate_report_content(description=bangla, media_count=0)


# ---------------------------------------------------------------------------------------
# Category hint (C-2, API §6.3)
# ---------------------------------------------------------------------------------------
def test_no_category_hint_leaves_the_report_unclassified() -> None:
    """The normal path — FR-10 has the LLM decide (BR-9)."""
    report = create_report(
        author=UserFactory.create(), location=IN_CITY, description=GOOD_DESCRIPTION
    )

    assert report.category is None
    assert report.classification_source is None


def test_a_citizen_category_hint_is_recorded_with_its_source() -> None:
    """FR-11 — a citizen's choice is honoured *and* attributed, not silently absorbed."""
    report = create_report(
        author=UserFactory.create(),
        location=IN_CITY,
        description=GOOD_DESCRIPTION,
        category_slug="roads",
    )

    assert report.category == Category.objects.get(slug="roads")
    assert report.classification_source == ClassificationSource.CITIZEN


def test_a_hinted_report_is_still_unclassified() -> None:
    """⚠️ A hint is not a classification — T3.5 must still pick this report up (BR-9)."""
    report = create_report(
        author=UserFactory.create(),
        location=IN_CITY,
        description=GOOD_DESCRIPTION,
        category_slug="roads",
    )

    assert report.is_classified is False
    assert report.classified_at is None
    assert report.severity_signal is None


def test_an_unknown_category_slug_is_rejected() -> None:
    """C-2 — the taxonomy is controlled; there are no free-form categories."""
    with pytest.raises(ReportValidationError):
        create_report(
            author=UserFactory.create(),
            location=IN_CITY,
            description=GOOD_DESCRIPTION,
            category_slug="not-a-real-category",
        )


def test_a_retired_category_hint_is_rejected_not_coerced_to_other() -> None:
    """⚠️ The `Other` coercion (BR-7, PRD §331) is for *LLM* output, not a human's choice.

    A machine returning something outside the taxonomy has given an unusable answer; a person
    picking a retired node is running a stale client. Filing their report under `Other` silently
    would lose information the caller could have corrected.
    """
    retired = RetiredCategoryFactory.create()

    with pytest.raises(ReportValidationError):
        create_report(
            author=UserFactory.create(),
            location=IN_CITY,
            description=GOOD_DESCRIPTION,
            category_slug=retired.slug,
        )


# ---------------------------------------------------------------------------------------
# Ordering of checks
# ---------------------------------------------------------------------------------------
def test_out_of_city_is_reported_before_a_short_description() -> None:
    """⚠️ Location is validated first, and the order is observable to a client.

    Both rules fail for this submission. Reporting the description problem first would send a
    citizen off to write more prose about a place UrbanMend does not serve — and they would
    still get a `422` on the retry.
    """
    with pytest.raises(OutOfCity):
        create_report(author=UserFactory.create(), location=OUT_OF_CITY_POINT, description="short")


def test_authorization_is_checked_before_any_validation() -> None:
    """A non-Citizen gets `403`, never a `422` that reveals which rule they also broke."""
    with pytest.raises(PermissionDenied):
        create_report(author=AuthorityFactory.create(), location=OUT_OF_CITY_POINT, description="")
