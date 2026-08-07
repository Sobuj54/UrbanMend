"""
Reporting — write operations (T2.1).

Every state change and every authorization check for this module lives here. This file
exists from day one even while empty: R-12 is the risk that "service-layer discipline
erodes under Django's idiom, scattering authorization into views/serializers", and the
named mitigation is that the convention is already in place, so putting a rule in a view
is never the path of least resistance.

Rules for this file [doc: Arch §3.1, FR-3]:
  - Callers pass the acting user; functions authorize before mutating. DRF permission
    classes are defence-in-depth, never the enforcement point.
  - Wrap multi-write operations in `transaction.atomic`.
  - Enqueue Celery tasks via `transaction.on_commit` so a worker cannot observe an
    uncommitted row [doc: Arch §2.4, §4.1].
  - Reads belong in selectors.py.

⚠️ **T2.1 is the entity plus its validation rules — not the endpoint.** `POST /reports` (T2.2)
and idempotency (T2.3) call `create_report()`; it is written to be callable without a request,
so a management command or the T2.2 view get the same rules (FR-3).

[doc: Arch §3 (FR-5, FR-8, FR-9, FR-11); BR-1..BR-3, BR-35; C-3, C-11]
"""

from __future__ import annotations

from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from urbenmend.api.exceptions import OutOfCity
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.geo.selectors import is_within_city
from urbenmend.identity.models import Role, User
from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus

# ⚠️ **BR-3's "adequate description" threshold, and it is a project decision, not doc-derived.**
# FR-5 and BR-3 both say "at least one of {photo, adequate description}" without defining
# adequate. 15 characters rejects "asdf" and "pothole" while accepting a real short sentence;
# it is deliberately low, because BR-3's purpose is to stop empty submissions, not to judge
# writing quality — and a citizen submitting a photo is exempt from it entirely.
#
# Anything stricter starts rejecting legitimate Bangla reports, which say more per character.
MIN_DESCRIPTION_LENGTH = 15


class ReportValidationError(ValidationError):
    """A T2.1 business-rule rejection (BR-2, BR-3).

    A Django `ValidationError` subclass so the view layer renders it as the `400`/`422` the
    envelope requires without this module importing DRF.
    """


def _resolve_category(slug: str | None) -> Category | None:
    """Resolve a client-supplied category *hint* to a taxonomy row (C-2, API §6.3).

    ⚠️ **A hint, not the classification.** API §6.3 marks `category` optional and says
    "client-supplied is a hint only" — FR-10 has the LLM decide. Recording it means FR-11's
    "citizens may override category at submission" is honoured and logged, not that triage is
    skipped.

    ⚠️ **Retired categories are refused, not coerced to `Other`.** The `Other` coercion in
    BR-7/PRD §331 is for an *LLM* returning something outside the taxonomy — a machine's
    unusable answer. A human explicitly picking a retired node is a stale client, and silently
    filing their report under `Other` loses information the caller could have corrected.
    """
    if slug is None:
        return None
    try:
        return Category.objects.get(slug=slug, status=CategoryStatus.ACTIVE)
    except Category.DoesNotExist as exc:
        raise ReportValidationError(
            {"category": f"'{slug}' is not an active category."},
            code="VALIDATION_FAILED",
        ) from exc


def validate_report_content(*, description: str, media_count: int) -> None:
    """BR-3 — at least one of {photo, adequate description} (FR-5).

    ⚠️ **`media_count` is passed in rather than counted here.** The `media` app does not exist
    until T2.4, and at intake the media rows are not yet attached to the report anyway — the
    client pre-uploads and sends `mediaIds` (API §6.3), so the count is only knowable to the
    caller. T2.6 wires the real number through; until then a caller supplying `0` gets the
    description rule, which is the honest answer for a text-only submission.
    """
    if media_count > 0:
        return
    if len(description.strip()) < MIN_DESCRIPTION_LENGTH:
        raise ReportValidationError(
            {
                "description": (
                    "Add a photo, or describe the problem in at least "
                    f"{MIN_DESCRIPTION_LENGTH} characters."
                )
            },
            code="VALIDATION_FAILED",
        )


def validate_location(*, location: Point | None) -> Point:
    """BR-2 + BR-35 — a location is required, and it must be inside the served city.

    ⚠️ **Raises two different exception types on purpose.** A missing location is a malformed
    request (`400 VALIDATION_FAILED`); a well-formed coordinate outside the city is a
    business-rule violation (`422 OUT_OF_CITY`), which is what API §6.3 and C-11 specify. A
    single exception type here would collapse the distinction the spec draws.
    """
    if location is None:
        raise ReportValidationError(
            {"location": "A location is required."},
            code="REQUIRED",
        )
    # ⚠️ Fails **closed**: an empty boundary table makes `is_within_city` answer `False`, so
    # every submission is rejected rather than silently accepted. Arch §409 sanctions the
    # opposite trade ("skip the out-of-city check" when the dependency is missing) — that
    # degradation is not taken here, because BR-35 is a hard constraint (C-11: a location
    # outside the city "is not accepted") and quietly disabling it leaves no trace. Operators
    # get a loud, uniform `422` and `active_city_boundary()` to diagnose it.
    if not is_within_city(location):
        raise OutOfCity
    return location


@transaction.atomic
def create_report(
    *,
    author: User,
    location: Point | None,
    description: str = "",
    category_slug: str | None = None,
    address: str = "",
    language: str = "en",
    media_count: int = 0,
) -> Report:
    """Create one report after enforcing every T2.1 rule (FR-5, BR-1/2/3/35).

    ⚠️ **Authorization: Citizen only** (API §6.3 "Authorization: Citizen"). Authorities and
    Admins are refused — their role on a report is to triage it, and an Authority filing into
    their own scope is a conflict of interest the ownership matrix does not grant (data-model
    "Ownership & Permissions" gives Authority `R⚙️/U⚙️` on Report, not `C`).

    ⚠️ **Does not enqueue classification.** T2.2 owns that, via `transaction.on_commit` so the
    worker cannot observe an uncommitted row (Arch §4.1, async-worker.md). Enqueuing here would
    put the side effect in the wrong task and make this function untestable without a broker.

    ⚠️ **`status` is `SUBMITTED`, never caller-supplied.** `PROCESSING` means "a classification
    job exists", which only T2.2 knows; accepting a status argument would let a caller mark a
    report `triaged` and skip the pipeline entirely.
    """
    if author.role != Role.CITIZEN:
        # Django's `PermissionDenied`, which the handler renders as the generic `403 FORBIDDEN`.
        # API §6.3 names no endpoint-specific code for this, and inventing one would put a
        # contract decision in a service (the reasoning T1.9 recorded for `MeView`).
        raise PermissionDenied("Only citizens may submit reports.")

    point = validate_location(location=location)
    validate_report_content(description=description, media_count=media_count)
    category = _resolve_category(category_slug)

    return Report.objects.create(
        author=author,
        description=description.strip(),
        category=category,
        location=point,
        address=address.strip(),
        language=language,
        status=ReportStatus.SUBMITTED,
        # ⚠️ Recorded only when the citizen actually chose one (FR-11: corrections are logged).
        # Setting it unconditionally would claim a human classified every report, and T3.5
        # would then have no way to find the ones still awaiting the LLM.
        classification_source=ClassificationSource.CITIZEN if category else None,
    )
