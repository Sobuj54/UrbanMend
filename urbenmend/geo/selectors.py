"""
Geospatial — read operations (T2.1).

Query functions for this module. Kept separate from services.py so reads never acquire
write-path side effects, and so the modules that consume this one have a single documented
surface to call [doc: Arch §3.1].

Rules for this file:
  - No writes, no `transaction.atomic`, no task enqueue.
  - Apply the caller's visibility rules here — a selector that returns rows the actor may
    not see is an authorization bug even though it wrote nothing [doc: Arch §3.1, FR-3].
  - Return querysets or domain objects, never DRF serializers or HTTP responses.

⚠️ `CityBoundary` is public reference data (data-model §16 grants every role `R`), so nothing
here filters by actor. The `Report`-level visibility rules live in `reporting/selectors.py`.

[doc: Arch §3 (FR-6, FR-16, FR-17, FR-23, NFR-1) and §9; data-model §16; BR-35, C-11]
"""

from __future__ import annotations

from django.contrib.gis.geos import Point

from urbenmend.geo.models import CityBoundary


class BoundaryUnavailable(RuntimeError):
    """No single active `CityBoundary` could be resolved.

    ⚠️ **Raised rather than returning `None`.** A `None` return invites
    `if boundary and not contains(...)`, which fails *open* — every out-of-city report is
    accepted the moment the table is empty, and BR-35 stops being enforced with nothing saying
    so. Arch §409 does sanction degrading gracefully when the boundary is missing ("skip the
    out-of-city check"), but that is a decision for a caller to take explicitly and visibly,
    not a default that arrives by way of a falsy value.
    """


def active_city_boundary() -> CityBoundary:
    """The single served-city boundary (data-model §16, ASSUMP-6).

    ⚠️ Raises `BoundaryUnavailable` unless exactly one active row exists. Two active boundaries
    is not a state to pick a winner from: they disagree about where the city ends, so row order
    would silently decide whether a report near the border is accepted.
    """
    boundaries = list(CityBoundary.objects.filter(is_active=True)[:2])
    if not boundaries:
        raise BoundaryUnavailable("No active city boundary is configured (BR-35, ASSUMP-6).")
    if len(boundaries) > 1:
        raise BoundaryUnavailable(
            "More than one active city boundary is configured; exactly one is expected."
        )
    return boundaries[0]


def is_within_city(point: Point) -> bool:
    """Whether `point` falls inside the served city (BR-35, C-11).

    ⚠️ **The containment test runs in PostGIS, not in Python.** `area.contains(point)` on a
    loaded instance uses GEOS, which treats the `geography` column as planar degrees, and it
    pulls the whole polygon into the process on every submission. `area__contains` hands the
    predicate to the database, which can use the GiST index.

    ⚠️ Callers must resolve `active_city_boundary()` first if they need the empty-table case to
    be an error: this function answers `False` for "outside the city" and for "no boundary
    configured" alike, and `create_report` must not reject every submission as out-of-city
    because reference data is missing.
    """
    return CityBoundary.objects.filter(is_active=True, area__contains=point).exists()
