# City boundary

The served-city polygon BR-35/C-11 validates report locations against.

## Current boundary

- `dhaka-demo.geojson` — a **stand-in** polygon approximating Dhaka's administrative extent,
  seeded by `geo` migration `0002_seed_city_boundary` as `Dhaka (development stand-in)` so the
  in-city check is testable end-to-end.
- ⚠️ **Not authoritative.** It was drawn for development, not sourced from a survey/GIS dataset.
  Replace it before any real deployment — see "Replacing the boundary" below.

## Replacing the boundary

The boundary is **data, not code** (NFR-11). Swap it without a code change:

1. Obtain an authoritative served-city polygon as GeoJSON. It must be a `MultiPolygon` —
   `CityBoundary.area` is a `MultiPolygonField` because real municipal boundaries are frequently
   discontiguous (enclaves, river islands). Wrap a single `Polygon` as a one-element multipolygon.
2. Add it as a **new row** via the Django admin (`CityBoundary`), SRID 4326.
3. Set the new row `is_active = true` and the stand-in `is_active = false`.

⚠️ **Add and retire — never edit the stand-in row in place, and never delete it**
(database.md "No hard deletes"). A boundary that has ever validated a report is the record of why
that report was accepted; overwriting its geometry makes a past `202`/`422` decision
unexplainable.

⚠️ **Exactly one row must be active.** That is deliberately not a database constraint —
`geo/selectors.py::active_city_boundary()` raises `BoundaryUnavailable` when the count is not one,
so the ambiguity surfaces as an error rather than a silent pick. Leaving two active rows fails
report submission loudly.

⚠️ **Do not re-point the migration at a new file.** `0002_seed_city_boundary` has been applied;
editing an applied migration is barred (database.md), and it would not re-run anyway.

## Source note

`ASSUMP-6` ("a city boundary polygon is available") is implied by the out-of-city edge case,
**not provided** — the PRD does not name a source. This stand-in unblocks M2's out-of-city
tests; the authoritative source remains open.
