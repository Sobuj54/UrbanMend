"""
Reporting — request/response shapes for `/reports` (T2.2 submit, T2.7 read, T2.8 edit).

⚠️ **The serializer validates *shape*; `services.py` enforces the *rules*** (FR-3, Arch §3.1).
BR-2/BR-3/BR-35 and the Citizen-only check all live in `submit_report()`, so a management command
or a future bulk importer gets them too. What is duplicated here is deliberate defence in depth:
a missing `location` is a field-level `400` with `details[].field == "location"` (API §4.1), which
is a better answer than the service's generic message — and the service still refuses `None`.

⚠️ **`ReportDetailSerializer` renders the same resource for the detail read, the list items and the
`PATCH` response.** §6.3 describes one Report shape; three serializers over one shape would be three
things free to drift, and the client-visible symptom is a field that appears on one endpoint and not
another with nothing in either to explain it.

[doc: API §6.3, §1.2, §1.3, §4.1, §4.4; FR-5, FR-11, FR-12; BR-2, BR-3, C-2]
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.contrib.gis.geos import Point
from rest_framework import serializers

from urbenmend.api.serializers import (
    CamelCaseSerializer,
    allowlisted_csv,
    reject_unknown_fields,
)
from urbenmend.classification.models import Category
from urbenmend.media.selectors import VISIBLE_MEDIA_ATTR, media_for_report
from urbenmend.media.serializers import MediaResponseSerializer
from urbenmend.reporting.models import Report, ReportStatus
from urbenmend.reporting.pagination import SORT_ASCENDING, SORT_CHOICES


class LocationSerializer(CamelCaseSerializer):
    """`{"lng": 90.399, "lat": 23.777}` (API §1.2 — coordinates are always this object).

    ⚠️ **Bounded to the WGS84 ranges, and that is not an invented policy number** — it is the
    definition of a degree coordinate. Without the bounds a `lat` of `200` builds a valid GEOS
    `Point`, falls outside the boundary polygon, and comes back as `422 OUT_OF_CITY` — telling a
    client with a transposed lat/lng that UrbanMend does not serve their city, which sends them
    looking in exactly the wrong place. Impossible coordinates are a malformed body (`400`); real
    coordinates elsewhere on Earth are the business-rule `422`. Same distinction T2.1 draws
    between `ReportValidationError` and `OutOfCity`.
    """

    lng = serializers.FloatField(min_value=-180.0, max_value=180.0)
    lat = serializers.FloatField(min_value=-90.0, max_value=90.0)

    @staticmethod
    def to_point(data: Mapping[str, float]) -> Point:
        """Build the SRID-4326 `Point` the model column expects.

        ⚠️ **`Point(lng, lat)` — x is longitude.** GeoJSON, PostGIS and GEOS all order `(x, y)`,
        while humans say "lat, long". Transposing them produces a structurally perfect point that
        lands in the Indian Ocean, so every submission is rejected `422 OUT_OF_CITY` and no test of
        shape, SRID or index would notice. `geo/tests/test_city_boundary.py` records the same trap
        on the boundary side.

        A `staticmethod` over a dict, not an instance method: a nested serializer never has its own
        `validated_data` populated, so reading `self.validated_data` here would raise `AssertionError`
        at runtime — the kind of bug a view-level "just build the Point inline" workaround invites,
        which is how the order ends up duplicated in two places.
        """
        return Point(data["lng"], data["lat"], srid=4326)

    @staticmethod
    def to_coordinates(point: Point) -> dict[str, float]:
        """The inverse of `to_point()`: the `{lng, lat}` object §1.2 requires, out of a `Point`.

        ⚠️ **`x` is longitude and `y` is latitude — the same trap as `to_point()`, read backwards.**
        Transposed, a response places every Issue and Report in the wrong hemisphere while every
        field name, type and range check still passes; a client map renders points in the Indian
        Ocean. Both directions of the conversion therefore live in this one class, so a reviewer
        comparing them has them side by side.
        """
        return {"lng": point.x, "lat": point.y}


class ReportSubmitSerializer(CamelCaseSerializer):
    """`POST /reports` request body (API §6.3).

    ⚠️ **A plain `Serializer`, never a `ModelSerializer` over `Report`.** A model serializer's
    field set follows the model, so `status`, `severity_signal`, `confidence`,
    `classification_source` and `classified_at` would each be one `fields` edit — or one
    `"__all__"` — away from being client-settable. Every one of those is derived data that
    api-conventions.md makes read-only to all clients. Same reasoning T1.9 recorded for
    `ProfileUpdateSerializer`.

    ⚠️ **`description` is uncapped here on purpose.** No doc fixes a maximum, and inventing one
    would start rejecting legitimate Bangla reports (which say more per character, the same reason
    `MIN_DESCRIPTION_LENGTH` is low). The real bound is Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` on
    the whole body; submission abuse is FR-33/T2.9's rate limit, not a field length.
    """

    description = serializers.CharField(required=False, allow_blank=True, default="")
    location = LocationSerializer()
    # ⚠️ A *hint*, and the field name is `category` because that is what §6.3 sends — but the
    # value is a `slug`, resolved by `_resolve_category()` against the active taxonomy (C-2).
    # `allow_blank=False`: `""` is not "no category", it is a client bug, and coercing it to
    # `None` would hide a broken picker.
    category = serializers.CharField(required=False, allow_null=True, allow_blank=False)
    # ⚠️ Declared, and now honoured: T2.4 uploads, T2.6 attaches. The ids are *shape*-checked here
    # (they must be UUIDs) and *resolved* in `media.services.resolve_media_for_attachment()`, which
    # is where ownership, single-use and the per-report cap live (FR-3).
    media_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    # No default: the view falls back to the author's `preferredLanguage` (FR-12), which is a
    # better answer for a Bangla-preferring citizen than the model's `"en"`.
    language = serializers.ChoiceField(choices=["en", "bn"], required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Reject fields this endpoint does not own.

        ⚠️ **Unknown fields are refused, not ignored — and here the neighbours are the reason.**
        The fields adjacent to this body are `severity_signal`, `status`, `confidence` and
        `issueId`: all derived, all read-only to every client (api-conventions.md). DRF's default
        would answer `POST {"severity":"critical"}` with a `202`, and the citizen would believe
        they had filed a Critical report. That is the same silent-success shape T1.9 refused on
        `PATCH /users/me`.

        The rule itself now lives in `api.serializers.reject_unknown_fields()` — three serializers
        in this module need it, and three copies of a security check are three chances to drift.
        """
        reject_unknown_fields(self)
        return attrs


class ReportSubmitResponseSerializer(CamelCaseSerializer):
    """`202` body for `POST /reports` (API §6.3).

        {"reportId": "…", "status": "processing", "issueId": null,
         "classification": {"state": "pending"}}

    ⚠️ **Serializes `services.SubmissionAcknowledgement`, not a `Report` — T2.3 changed this.**
    §6.3 as amended says an `Idempotency-Key` replay "returns this same `202` body verbatim", and a
    replay has no live row to read: what it has is the acceptance record the original request
    produced. Rendering a `Report` on the fresh path and a stored dict on the replay path would put
    the two bodies in two different pieces of code, free to drift — the client-visible symptom being
    a retry that looks like a *different* submission. One dataclass through one serializer makes them
    identical by construction.

    ⚠️ **The four §6.3 fields, and nothing else.** `SubmissionAcknowledgement` also carries
    `replayed`, which is deliberately not declared here: it is signalled by the
    `Idempotency-Replayed` header (§4.6), and the bodies must stay byte-identical or the header
    would be redundant and the "verbatim" guarantee false. A test asserts the exact key set.

    The derivations these fields used to compute — `issueId` always `null` in the pre-worker
    acceptance snapshot (BR-6), `classification.state` from `is_classified` rather than a literal
    `"pending"` (BR-9), and `status` read off the row instead of a hardcoded `"processing"` (the T2.2
    decision) — now live in `services._acknowledge()`, with their reasoning. They moved because they
    must be evaluated **at acceptance time**: by the time a replay arrives, triage may have finished,
    and re-deriving them then would answer a `202` with post-acceptance state that §6.3 sends clients
    to `GET /reports/{id}` for.
    """

    # `CharField`, not `UUIDField(source="pk")`: the acknowledgement already holds a `str`, which is
    # also what survives a round-trip through the idempotency cache. A `UUID` there would be a type
    # the contract does not describe and JSON cannot carry.
    report_id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    # Nullable output: DRF's `Serializer.to_representation` emits `None` without calling the field,
    # so no `allow_null` is needed and `str(None)` can never leak as `"None"`.
    issue_id = serializers.CharField(read_only=True)
    classification = serializers.DictField(child=serializers.CharField(), read_only=True)


class ReportPatchSerializer(CamelCaseSerializer):
    """`PATCH /reports/{id}` request body (API §6.3, FR-11).

        {"description": "…", "category": "water_drainage"}

    ⚠️ **Both fields are `required=False` and neither accepts `null`, and that pair is what makes
    partial-update semantics expressible.** `PATCH` means "change what I sent" (api-conventions.md),
    so absence has to be distinguishable from a value — and `description: ""` is a *real* value a
    client may send, because BR-3 accepts a report carried by its photo alone. `update_report()`
    reads `None` as "not sent", which only works because an explicit `null` cannot get past here.

    ⚠️ **`category: null` is refused rather than treated as "clear it".** Clearing a category is not
    an edit: it is a request to re-run triage, which this endpoint does not do and which would leave
    `classification_source` naming a human who no longer has a decision attached. A client that wants
    a different category sends one.

    ⚠️ **No `severity` field, and `reject_unknown_fields()` is why sending one fails loudly.** FR-11
    says authorities may "re-severity", but severity lives on the **Issue**, never on the Report
    (CLAUDE.md, data-model "Ownership & Permissions"), so that override is §6.5's endpoint. DRF's
    default would drop the key and answer `200`, and an Authority would believe they had just
    escalated a Critical report — the same silent-success shape T1.9 refused on `PATCH /users/me`.
    `status`, `confidence` and `issueId` are refused by the same call, for the same reason.

    ⚠️ **An empty body is `400`, not a `200` no-op.** A `PATCH {}` that answers `200` with the
    unchanged resource is indistinguishable from a successful edit, so a client whose field name is
    wrong (and therefore stripped by its own serialization) would see every edit "succeed" and
    nothing change. `update_report()` carries the same guard for non-HTTP callers.
    """

    description = serializers.CharField(required=False, allow_blank=True, allow_null=False)
    # The value is a `slug`, resolved against the *active* taxonomy by `_resolve_category()` — the
    # same resolver intake uses, so a retired slug is refused on both paths (C-2, T2.1).
    category = serializers.CharField(required=False, allow_blank=False, allow_null=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        if not attrs:
            raise serializers.ValidationError(
                "Send a description or a category to change.", code="REQUIRED"
            )
        return attrs


class ReportDetailSerializer(CamelCaseSerializer):
    """The Report resource, for `GET /reports/{id}`, `GET /reports[]` and `PATCH` (API §6.3, T2.7).

    ⚠️ **One serializer for all three, because §6.3 shows one resource shape.** A separate, thinner
    list item would be free to drift from the detail body, and the client-visible symptom is a
    field that exists when you fetch one report and vanishes when you list them — with nothing in
    either endpoint's code to explain it. The cost is that a list page carries `media[]` for every
    row, which is why `list_reports()` prefetches it in a single query.

    ⚠️ **Not a `ModelSerializer`, and here the neighbours are the reason.** The columns beside these
    are `classification_rationale`, `classification_model`, `updated_at` and `language`. §6.3's
    `classification` object has exactly four keys; the rationale belongs to the Issue view (FR-15),
    and one `fields` edit — or one `"__all__"` — would publish the LLM's raw reasoning about a
    citizen's photo on a public endpoint. Same reasoning `ReportSubmitSerializer` and
    `MediaResponseSerializer` both record.

    ⚠️ **`authorId` is in a public body because §6.3 puts it there** — the spec is authoritative
    (api-conventions.md), and the value is an opaque non-sequential UUID carrying no contact detail
    (auth.md: "the API never returns another user's contact info"). It is a correlation handle, not
    an identity; do not "fix" it by dropping the field without amending the spec first.

    ⚠️ **`location` nests `address`, and that is §6.3's shape, not a tidier one.** The column is
    top-level on the model, so the natural `ModelSerializer` rendering would put `address` beside
    `description` — a body that reads fine and matches no client.
    """

    id = serializers.CharField(read_only=True)
    author_id = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    location = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()
    classification = serializers.SerializerMethodField()
    issue_id = serializers.SerializerMethodField()
    status = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)

    def get_location(self, report: Report) -> dict[str, Any]:
        """`{"lng": …, "lat": …, "address": …}` (API §1.2, §6.3).

        The coordinate pair comes from `LocationSerializer.to_coordinates()` so the `x`-is-longitude
        decision has exactly one implementation to review — see that method for what a transposition
        looks like from the outside. `address` is this resource's own addition to the §1.2 object.

        `address` is `""` rather than `null` when reverse geocoding was unavailable (ASSUMP-5,
        Arch §4.9): the column is `blank=True` with no `null`, so one spelling of empty.
        """
        return {**LocationSerializer.to_coordinates(report.location), "address": report.address}

    def get_media(self, report: Report) -> list[dict[str, Any]]:
        """§6.3's `media[]`, moderated rows already excluded by the selector.

        ⚠️ **Reads the prefetched attribute when it is there and falls back to a query when it is
        not.** Both selectors attach `visible_media_prefetch()`, so the fallback is for a caller
        that renders this serializer over a `Report` it fetched itself — a test, a management
        command, T2.8's post-update render. Without it that caller gets an `AttributeError`, i.e. a
        `500`, from a *read* of a perfectly valid row.
        """
        visible = getattr(report, VISIBLE_MEDIA_ATTR, None)
        if visible is None:
            visible = media_for_report(report_id=report.pk)
        return list(MediaResponseSerializer(visible, many=True).data)

    def get_classification(self, report: Report) -> dict[str, Any]:
        """§6.3's four keys — all `null` until T3.5's worker writes them (BR-9).

        ⚠️ **`category` is the slug, not the label or the id.** T0.10 makes the slug the machine key
        and §6.2 quotes `roads`/`water_drainage`/`electrical` as contract values; emitting the
        bilingual display label here would make the field unusable as a filter value on the very
        next request (`?category=`).

        ⚠️ **`or None` on the two choice columns, deliberately.** Both are nullable `CharField`s, so
        "unclassified" is `NULL` — but a `""` written by any path would serialize as an empty string,
        which is an undeclared fifth `severitySignal` on the wire and would be read by a client as a
        value rather than an absence. The model's `noqa: DJ001` comment records the same trap on the
        storage side.
        """
        # Bound to a local rather than read twice: `report.category` is a related descriptor, and the
        # `if`/`else` form over the `category_id` column cannot narrow it for a type-checker. Both
        # spellings provoke the same (single, prefetched) query.
        category = report.category
        return {
            "category": category.slug if category else None,
            "severitySignal": report.severity_signal or None,
            "confidence": report.confidence,
            "source": report.classification_source or None,
        }

    def get_issue_id(self, report: Report) -> str | None:
        """The opaque Issue id once clustering attaches this Report, otherwise `null`.

        ⚠️ **Declared from the initial contract rather than added with clustering, because §6.3 lists
        it** and making clients start handling a new key later would be a breaking change dressed as
        an additive one. Reading the raw `issue_id` avoids loading the related Issue just to serialize
        its key.
        """
        return str(report.issue_id) if report.issue_id else None


class ReportListQuerySerializer(CamelCaseSerializer):
    """`GET /reports` query parameters (API §6.3, §4.4).

    ⚠️ **Validating query params in a serializer at all is the point.** api-conventions.md fixes
    `400` for an unknown param, and the alternative — reading `request.query_params.get()` in the
    view — silently ignores `?statuss=triaged` and answers `200` with the *unfiltered* list. A
    citizen filtering for their own open reports would be shown all of them and have no way to tell.

    ⚠️ **An invalid *value* is a `400` too, not an empty page.** `?status=bogus` and
    `?category=potholes` (the slug is `roads`) would each match nothing, and "no results" is
    indistinguishable from "you asked the wrong question". Both enums are public — §6.13's
    `/meta/enums` and the taxonomy — so naming the offending value leaks nothing.
    """

    status = serializers.CharField(required=False)
    category = serializers.CharField(required=False)
    q = serializers.CharField(required=False)
    # Same degree bounds and the same reasoning as `LocationSerializer`: an impossible coordinate is
    # a malformed request (`400`), not a search that happens to find nothing.
    near_lng = serializers.FloatField(required=False, min_value=-180.0, max_value=180.0)
    near_lat = serializers.FloatField(required=False, min_value=-90.0, max_value=90.0)
    radius_m = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=float(settings.REPORT_SEARCH_MAX_RADIUS_M),
    )
    sort = serializers.ChoiceField(choices=list(SORT_CHOICES), required=False)

    # ⚠️ The paginator's params, allowed here because `reject_unknown_fields()` only knows about
    # this serializer's fields. Omitting them makes `?cursor=…` — i.e. every page after the first —
    # a `400`, and the first page would still work, so a naive test would not catch it.
    PAGINATION_PARAMS = ("limit", "cursor")

    def validate_status(self, value: str) -> list[str]:
        """`?status=submitted,triaged` → `["submitted", "triaged"]` (§4.4 comma-separated)."""
        return self._allowlisted(value, allowed=set(ReportStatus.values), label="status")

    def validate_category(self, value: str) -> list[str]:
        """`?category=roads,water_drainage` → slugs, checked against the taxonomy (C-2).

        ⚠️ **Retired categories are accepted here, unlike on submission.** `_resolve_category()`
        refuses a retired slug because a *new* report must not be filed against a closed category
        (T2.1); a report classified before the retirement still points at it, and refusing the slug
        as a filter would make those reports unfindable. Lifecycle is `Active → Retired`, never
        deleted (T0.10), precisely so this stays queryable.
        """
        slugs = set(Category.objects.values_list("slug", flat=True))
        return self._allowlisted(value, allowed=slugs, label="category")

    @staticmethod
    def _allowlisted(value: str, *, allowed: set[str], label: str) -> list[str]:
        # Delegates to the shared §4.4 helper — `GET /issues` needs the identical split-and-check,
        # and two implementations would agree until one of them grew a `strip()` the other lacked.
        # Kept as a staticmethod so the two `validate_*` call sites above read unchanged.
        return allowlisted_csv(value, allowed=allowed, label=label)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Unknown params, and the all-or-nothing spatial triple."""
        reject_unknown_fields(
            self,
            extra_allowed=self.PAGINATION_PARAMS,
            message="This query parameter is not accepted by this endpoint.",
        )

        # ⚠️ **All three spatial params or none — a partial triple is refused, never defaulted.**
        # §6.3 documents them as one unit. Two plausible "helpful" readings both silently answer the
        # wrong question: treating a missing `radiusM` as a default radius invents a policy number
        # the spec does not have, and ignoring a lone `nearLng` returns the whole city to a client
        # that believes it asked for one street corner.
        spatial = {"nearLng": "near_lng", "nearLat": "near_lat", "radiusM": "radius_m"}
        present = {sent: attr for sent, attr in spatial.items() if attrs.get(attr) is not None}
        if present and len(present) != len(spatial):
            missing = sorted(set(spatial) - set(present))
            raise serializers.ValidationError(
                dict.fromkeys(missing, "Required together with nearLng, nearLat and radiusM."),
                code="REQUIRED",
            )
        return attrs

    def to_point(self) -> Point | None:
        """The search centre, or `None` when this request is not a spatial one.

        Reuses `LocationSerializer.to_point()` so the `(lng, lat)` argument order stays defined in
        exactly one place — the trap that module records at length.
        """
        data = self.validated_data
        if data.get("near_lng") is None:
            return None
        return LocationSerializer.to_point({"lng": data["near_lng"], "lat": data["near_lat"]})

    @property
    def ascending(self) -> bool:
        """Whether `?sort=` asked for oldest-first. Default is `-createdAt` (api-conventions.md)."""
        return bool(self.validated_data.get("sort") == SORT_ASCENDING)
