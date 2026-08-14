"""
Geospatial — persistence (T2.1).

Spatial queries (radius, nearest-POI, density), reverse geocoding integration, and POI reference
data. `CityBoundary` supplies the polygon for BR-35/C-11; `POI` supplies display-only proximity
context for Issues (T4.8).

⚠️ **This is the project's first geometry-bearing app.** `identity/0001` runs
`CreateExtension("postgis")` as its first operation and `geo/0001` names it in `dependencies` —
Django orders migrations by the dependency graph, not by app name, so without that edge the
geometry column can be created before the extension exists [doc: CLAUDE.md A7].

[doc: Arch §3 (FR-6, FR-16, FR-17, FR-23, NFR-1) and §9; data-model §16; BR-35, C-11]
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GistIndex
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class CityBoundary(models.Model):
    """The served city's outline, used to reject out-of-city reports (BR-35, C-11).

    **Reference data, Admin-managed** (data-model §16, BR-34/C-13, NFR-11). The polygon is
    seeded by a migration and editable by an Admin — never hard-coded as coordinates in Python,
    which would make redrawing a city boundary a code deploy.

    ⚠️ **`MultiPolygonField`, not `PolygonField`.** A real municipal boundary is frequently
    discontiguous — enclaves, river islands, detached wards — and a single `Polygon` cannot
    represent that. Loading such a boundary into a `PolygonField` fails at import time, long
    after the schema is frozen; accepting one polygon as a one-element multipolygon costs
    nothing now.

    ⚠️ **`geography=True`, SRID 4326**, matching `Report.location` (Arch §9, database.md). A
    mismatch between the two would make `ST_Within` compare a geography against a geometry and
    either error or silently compute in degrees.

    **Single city now, latent multi-city** (ASSUMP-6/8, PRD §11). `is_active` rather than a
    `city` FK: the model must not *prevent* a future city column, but adding one now would build
    the multi-tenancy PRD §2.2 lists as a non-goal.

    ⚠️ **Rows are retired, never deleted** (database.md "No hard deletes"). A boundary that has
    ever validated a report is the record of why that report was accepted; deleting it makes a
    past `422`/`202` decision unexplainable.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Human label only — the served city is identified by `is_active`, not by name matching.
    name = models.CharField(max_length=100, unique=True)

    # ⚠️ The authoritative geometry for BR-35. GiST-indexed via `Meta.indexes` below rather
    # than the implicit `spatial_index=True`, so the index name is explicit and stable
    # (database.md: "GiST spatial indexes on every queried geometry column").
    area = gis_models.MultiPolygonField(
        geography=True,
        srid=4326,
        spatial_index=False,
        help_text=_("The served city outline. Reports outside it are rejected (BR-35)."),
    )

    # ⚠️ Exactly one row should be active at a time, but that is deliberately NOT a database
    # constraint: swapping boundaries would then require a single transaction that deactivates
    # and activates in the right order, and a partial-unique index makes the *first* insert the
    # awkward case. `active_city_boundary()` in selectors.py resolves the active row and raises
    # when the count is not one, so the ambiguity surfaces as an error rather than a silent pick.
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text=_("Whether this is the currently served boundary. Retire rather than delete."),
    )

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "geo_city_boundary"
        verbose_name = _("city boundary")
        verbose_name_plural = _("city boundaries")
        ordering = ["name"]
        indexes: ClassVar[list[models.Index]] = [
            # ⚠️ **`GistIndex`, not `models.Index`.** `gis_models.Index` is just an alias for
            # `models.Index`, which emits a plain B-tree — useless for `ST_Within`, and it would
            # have made the containment check a sequential scan while looking indexed in the
            # migration. `spatial_index=False` on the field above is what stops Django adding a
            # second, auto-named GiST index alongside this one (database.md, NFR-1).
            GistIndex(fields=["area"], name="geo_city_boundary_area_gist"),
        ]

    def __str__(self) -> str:
        return self.name


class POIType(models.TextChoices):
    """Controlled point-of-interest vocabulary from the v1 API contract."""

    HOSPITAL = "hospital", _("Hospital")
    SCHOOL = "school", _("School")
    HIGHWAY = "highway", _("Highway")
    MARKET = "market", _("Market")


class POI(models.Model):
    """Admin-managed point of interest used only as display context (FR-17, C-10).

    POIs are queried at read time. There is deliberately no Issue relationship or cached
    proximity value: changing reference data must not rewrite Issues, and proximity must never
    affect severity, queue ordering, clustering, or any other business decision.

    Rows are retired with `is_active=False`, never hard-deleted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    poi_type = models.CharField(max_length=20, choices=POIType.choices)
    location = gis_models.PointField(geography=True, srid=4326, spatial_index=False)
    source = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "geo_poi"
        verbose_name = _("point of interest")
        verbose_name_plural = _("points of interest")
        ordering = ["name", "pk"]
        indexes: ClassVar[list[models.Index]] = [
            GistIndex(fields=["location"], name="geo_poi_location_gist"),
            models.Index(fields=["poi_type", "is_active"], name="geo_poi_type_active_idx"),
        ]

    def __str__(self) -> str:
        return self.name
