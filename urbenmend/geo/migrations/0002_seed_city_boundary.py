"""Seed the served-city boundary from `docs/city-boundary/` (T2.1, BR-35, ASSUMP-6).

⚠️ **The polygon is read from a GeoJSON file, not inlined as coordinates.** Taxonomy, POIs and
boundaries are reference *data*, not code (NFR-11, BR-34) — a boundary redraw must be a data
change an Admin can make, not a code deploy. `docs/city-boundary/README.md` documents the swap.

⚠️ **The shipped polygon is a development stand-in, not an authoritative city outline.**
ASSUMP-6 says a boundary "is available"; the PRD names no source, so one was drawn to make the
BR-35 path testable end-to-end. Replacing it is a documented operation, not a migration edit.

⚠️ **`apps.get_model`, and no import from application code** — including `services.py` /
`selectors.py` (database.md). A data migration that imports live code breaks the day that code
changes, replaying history against a model that no longer matches.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.db import migrations

# The stand-in is committed under `docs/` rather than inside the package: it is documentation
# of a decision plus its data, and `settings.BASE_DIR` is not importable from a migration
# without coupling this file to settings.
BOUNDARY_FILE = (
    Path(__file__).resolve().parents[3] / "docs" / "city-boundary" / "dhaka-demo.geojson"
)
BOUNDARY_NAME = "Dhaka (development stand-in)"


def _load_multipolygon() -> Any:
    """Read the GeoJSON `MultiPolygon` as a GEOS geometry.

    ⚠️ Imported inside the function, not at module scope: `django.contrib.gis.geos` needs GEOS
    loaded, and a module-level import makes the whole migration module unimportable on a machine
    without the library — including for `makemigrations --check`, which touches no database.
    """
    from django.contrib.gis.geos import GEOSGeometry

    document = json.loads(BOUNDARY_FILE.read_text(encoding="utf-8"))
    geometry = document["features"][0]["geometry"]
    return GEOSGeometry(json.dumps(geometry), srid=4326)


def seed_boundary(apps: Any, schema_editor: Any) -> None:
    CityBoundary = apps.get_model("geo", "CityBoundary")
    CityBoundary.objects.update_or_create(
        name=BOUNDARY_NAME,
        defaults={"area": _load_multipolygon(), "is_active": True},
    )


def unseed_boundary(apps: Any, schema_editor: Any) -> None:
    """⚠️ A real reverse (database.md: `RunPython` is not reversible without one).

    Deletes **only the seeded row by name** — never `CityBoundary.objects.all()`, which would
    take an operator's hand-imported real boundary with it on a routine down-migration.
    """
    CityBoundary = apps.get_model("geo", "CityBoundary")
    CityBoundary.objects.filter(name=BOUNDARY_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("geo", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_boundary, unseed_boundary),
    ]
