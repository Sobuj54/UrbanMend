"""Issue status, assignment, severity, merge/split and confirmation serializers (T4.7-T5.7),
plus the `GET /issues` work-queue resource and its filter allowlists (T7.1/T7.2)."""

from typing import Any

from django.conf import settings
from django.contrib.gis.geos import Point, Polygon
from django.utils import timezone
from rest_framework import serializers

from urbenmend.api.serializers import (
    CamelCaseSerializer,
    allowlisted_csv,
    reject_unknown_fields,
)
from urbenmend.classification.models import Category
from urbenmend.geo.models import POI
from urbenmend.geo.selectors import nearby_pois
from urbenmend.issues.models import Comment, CommentVisibility, Issue, IssueStatus, StatusEvent
from urbenmend.issues.models import ClusteringRule, ClusteringRuleStatus
from urbenmend.issues.pagination import SORT_CHOICES
from urbenmend.issues.selectors import MODERATED_ISSUE_STATUSES, PROXIMITY_ATTR
from urbenmend.issues.services import REOPEN_ACTION
from urbenmend.reporting.models import SeveritySignal
from urbenmend.reporting.serializers import LocationSerializer


class IssueStatusTransitionSerializer(CamelCaseSerializer):
    """`PATCH /issues/{id}/status` request body (API section 6.5)."""

    to_status = serializers.ChoiceField(choices=[*IssueStatus.values, REOPEN_ACTION])
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    public_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    duplicate_of_issue_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        to_status = attrs.get("to_status")
        duplicate_id = attrs.get("duplicate_of_issue_id")
        if to_status == IssueStatus.DUPLICATE and duplicate_id is None:
            raise serializers.ValidationError(
                {"duplicate_of_issue_id": "This field is required for duplicate transitions."}
            )
        if to_status != IssueStatus.DUPLICATE and duplicate_id is not None:
            raise serializers.ValidationError(
                {"duplicate_of_issue_id": "This field is accepted only for duplicate transitions."}
            )
        return attrs


class IssueStatusResponseSerializer(CamelCaseSerializer):
    """Status mutation result; the full Issue read resource lands with T7.3."""

    issue_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=IssueStatus.choices)
    duplicate_of_issue_id = serializers.UUIDField(allow_null=True)
    reopened_from_issue_id = serializers.UUIDField(allow_null=True)


class IssueAssignmentSerializer(CamelCaseSerializer):
    """`PATCH /issues/{id}/assignment` request body (API section 6.5)."""

    assignee_id = serializers.UUIDField(allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs

class ClusteringRuleSerializer(CamelCaseSerializer):
    id = serializers.IntegerField(read_only=True)
    category = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    radius_m = serializers.IntegerField(read_only=True)
    time_window_hours = serializers.IntegerField(read_only=True)
    active = serializers.SerializerMethodField()
    def get_active(self, obj: ClusteringRule) -> bool: return obj.status == ClusteringRuleStatus.ACTIVE

class ClusteringRuleWriteSerializer(CamelCaseSerializer):
    category = serializers.SlugField(required=False)
    radius_m = serializers.IntegerField(required=False, min_value=1)
    time_window_hours = serializers.IntegerField(required=False, min_value=1)
    active = serializers.BooleanField(required=False)
    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        if not attrs: raise serializers.ValidationError("Provide at least one field.")
        return attrs

class StatusEventSerializer(CamelCaseSerializer):
    from_status = serializers.CharField(read_only=True)
    to_status = serializers.CharField(read_only=True)
    actor_role = serializers.CharField(source="actor.role", read_only=True)
    reason = serializers.SerializerMethodField()
    at = serializers.DateTimeField(source="created_at", read_only=True)
    def get_reason(self, obj: StatusEvent) -> str | None:
        return obj.reason or None
    def to_representation(self, instance: StatusEvent) -> dict[str, Any]:
        data = super().to_representation(instance)
        data["from"] = data.pop("fromStatus")
        data["to"] = data.pop("toStatus")
        return data


class IssueAssignmentResponseSerializer(CamelCaseSerializer):
    """Assignment mutation result; the full Issue resource lands with T7.3."""

    issue_id = serializers.UUIDField()
    assignee_id = serializers.UUIDField(allow_null=True)


class IssueSeverityOverrideSerializer(CamelCaseSerializer):
    """`PATCH /issues/{id}/severity` request body (API section 6.5)."""

    # Business-rule errors are 422, so presence/band/reason validation lives in the service.
    severity = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class IssueSeverityStateSerializer(CamelCaseSerializer):
    computed = serializers.ChoiceField(choices=SeveritySignal.choices)
    computed_rationale = serializers.CharField()
    overridden = serializers.ChoiceField(choices=SeveritySignal.choices)
    current = serializers.ChoiceField(choices=SeveritySignal.choices)
    override_reason = serializers.CharField()
    overridden_by = serializers.UUIDField()
    overridden_at = serializers.DateTimeField()


class IssueSeverityResponseSerializer(CamelCaseSerializer):
    """The preserved computed value and current human override (BR-20/21)."""

    issue_id = serializers.UUIDField()
    severity = IssueSeverityStateSerializer(source="*")


class IssueMergeSerializer(CamelCaseSerializer):
    """`POST /issues/{id}/merge` request body (API section 6.5)."""

    merge_with_issue_id = serializers.UUIDField()
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class IssueMergeResponseSerializer(CamelCaseSerializer):
    """Compact surviving Issue resource until the full T7.3 serializer exists."""

    issue_id = serializers.UUIDField()
    merged_issue_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=IssueStatus.choices)
    computed_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    current_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    report_count = serializers.IntegerField(min_value=1)
    corroboration_count = serializers.IntegerField(min_value=0)


class IssueSplitSerializer(CamelCaseSerializer):
    """`POST /issues/{id}/split` request body (API section 6.5)."""

    report_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=True)
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class SplitIssueStateSerializer(CamelCaseSerializer):
    issue_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=IssueStatus.choices)
    computed_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    current_severity = serializers.ChoiceField(choices=SeveritySignal.choices)
    report_count = serializers.IntegerField(min_value=1)
    corroboration_count = serializers.IntegerField(min_value=0)


class IssueSplitResponseSerializer(CamelCaseSerializer):
    original = SplitIssueStateSerializer()
    created = SplitIssueStateSerializer()


class ConfirmationCreateSerializer(CamelCaseSerializer):
    """The endpoint accepts an empty object and refuses invented client-owned fields."""

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class ConfirmationResponseSerializer(CamelCaseSerializer):
    """`201` body after creating one confirmation."""

    issue_id = serializers.UUIDField()
    corroboration_count = serializers.IntegerField(min_value=0)


class CommentSerializer(CamelCaseSerializer):
    author_id = serializers.UUIDField(source="author_id", read_only=True)
    visibility = serializers.ChoiceField(choices=CommentVisibility.choices)
    body = serializers.CharField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Comment
        fields = ["id", "author_id", "body", "visibility", "created_at", "updated_at"]


class CommentCreateSerializer(CamelCaseSerializer):
    body = serializers.CharField()
    visibility = serializers.ChoiceField(
        choices=CommentVisibility.choices, default=CommentVisibility.PUBLIC
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


class CommentUpdateSerializer(CamelCaseSerializer):
    body = serializers.CharField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(self)
        return attrs


def _poi_context(poi: POI) -> dict[str, Any]:
    """One `proximity[]` entry — display-only Issue context (§6.5, FR-17, C-10).

    ⚠️ **`distance` is a `django.contrib.gis.measure.Distance`, not a number.** `nearby_pois()`
    annotates it with `ST_Distance`, and GeoDjango's `DistanceField` wraps the result — rendering the
    object directly would emit `"120.0 m"` or a repr depending on the encoder, on a field the contract
    types as an integer. `.m` is the conversion, done here so no caller has to remember it.

    ⚠️ **`getattr` with a default rather than `poi.distance`.** django-stubs knows the model's columns
    and not its annotations, so the attribute access alone fails type-checking; the default also keeps
    a hand-fetched `POI` (a test, a management command) rendering as `distanceM: null` instead of
    raising a `500` from a read.

    ⚠️ **The keys are hand-written camelCase.** `CamelCaseSerializerMixin` renames a serializer's
    *declared* fields, and this dict is a `SerializerMethodField` return value — so nothing downstream
    will turn `poi_type` into `poiType` (the trap `get_classification` records).
    """
    distance = getattr(poi, "distance", None)
    return {
        "poiType": poi.poi_type,
        "name": poi.name,
        "distanceM": None if distance is None else round(distance.m),
    }


class IssueQueueItemSerializer(CamelCaseSerializer):
    """One row of the authority work queue (API §6.5's `data[]`, FR-22).

    ⚠️ **A plain `Serializer`, never a `ModelSerializer`, and the neighbouring columns are why.**
    Beside these fields sit `severity_override_reason`, `severity_overridden_by`,
    `severity_overridden_at`, `duplicate_of` and `reopened_from`. §6.5's `severity` object has exactly
    four keys and the *override reason* is not one of them — one `fields` edit, or one `"__all__"`,
    would publish an Authority's internal justification on a public endpoint (Q7: this list is public).
    Same reasoning `ReportDetailSerializer` records.

    ⚠️ **Declaration order follows §6.5's example body.** The response is JSON so order carries no
    meaning to a client, but a diff against the spec is how this stays honest, and reordering makes
    that diff unreadable.

    ⚠️ **The three derived numbers read the annotation first and the property second.** `list_issues()`
    annotates `corroboration_total`/`report_total` so they can be sorted and paged on; the property
    fallback is for a caller rendering an `Issue` it fetched itself, which would otherwise get an
    `AttributeError` — i.e. a `500` — from a read of a perfectly valid row (the `get_media` precedent).
    """

    id = serializers.UUIDField(read_only=True)
    # The slug, not the label or the id: it is the machine key (T0.10) and the value a client feeds
    # straight back as `?category=`. A bilingual label would be unusable as the next request's filter.
    primary_category = serializers.CharField(source="primary_category.slug", read_only=True)
    severity = serializers.SerializerMethodField()
    status = serializers.CharField(read_only=True)
    assigned_to = serializers.SerializerMethodField()
    corroboration_count = serializers.SerializerMethodField()
    proximity = serializers.SerializerMethodField()
    representative_location = serializers.SerializerMethodField()
    report_count = serializers.SerializerMethodField()
    opened_at = serializers.DateTimeField(read_only=True)
    age_seconds = serializers.SerializerMethodField()

    def get_severity(self, issue: Issue) -> dict[str, Any]:
        """§6.5's four keys — `{current, computed, overridden, rationale}` and nothing else.

        ⚠️ **`rationale` is the *computed* rationale (FR-15), not the override reason.** The two are
        different columns with different audiences: the rationale explains the triage decision and is
        public, the override reason is an Authority's note about overruling it. Rendering the latter
        here is a one-word change that leaks internal deliberation on a public list.

        ⚠️ **`overridden_severity or None` — `""` must never reach the wire.** The column is
        `null=True, blank=True` with a deliberate `# noqa: DJ001`, so both spellings of "no override"
        exist in the table; §6.5 shows `null`, and an empty string is an undeclared fifth severity
        band as far as a client's enum parser is concerned.

        `current` reads the model property rather than recomputing the `Coalesce`, so the displayed
        band has exactly one definition.
        """
        return {
            "current": issue.current_severity,
            "computed": issue.computed_severity,
            "overridden": issue.overridden_severity or None,
            "rationale": issue.computed_severity_rationale,
        }

    def get_assigned_to(self, issue: Issue) -> str | None:
        """The assignee's opaque id, or `null` when unassigned.

        ⚠️ **Reads `assignee_id`, not `assignee.pk`.** The column is already on the row, while the
        attribute access lazy-loads the related object — turning a 20-row page into 20 extra queries
        the moment a caller forgets `select_related`. `list_issues()` deliberately does *not* join
        `assignee` for exactly this reason: nothing here needs a second column from that table.
        """
        return str(issue.assignee_id) if issue.assignee_id else None

    def get_corroboration_count(self, issue: Issue) -> int:
        """FR-16's distinct corroborating people — derived, read-only to every client."""
        total = getattr(issue, "corroboration_total", None)
        return issue.corroboration_count if total is None else int(total)

    def get_report_count(self, issue: Issue) -> int:
        """Member Reports in this cluster."""
        total = getattr(issue, "report_total", None)
        return issue.report_count if total is None else int(total)

    def get_representative_location(self, issue: Issue) -> dict[str, float]:
        """`{"lng": …, "lat": …}` (§1.2), via the one helper that knows `x` is longitude."""
        return LocationSerializer.to_coordinates(issue.representative_location)

    def get_proximity(self, issue: Issue) -> list[dict[str, Any]]:
        """Nearby POIs, display-only (FR-17, C-10).

        ⚠️ **`attach_proximity()` populates this before the serializer runs, and the fallback query
        is for callers outside the view.** The view attaches over the page's rows only — which is what
        keeps POI data out of anything that filters or orders the collection (C-10) and bounds the cost
        to the page. A caller that skipped the helper gets a correct answer rather than an empty list.

        ❓**Q3 (POI source) is open, so the table is empty and this is `[]` on every row today.** The
        field is present and correct; it populates when Q3 resolves. Not worked around, just unfed.
        """
        attached = getattr(issue, PROXIMITY_ATTR, None)
        if attached is None:
            attached = nearby_pois(
                point=issue.representative_location,
                radius_m=float(settings.ISSUE_PROXIMITY_RADIUS_M),
                limit=int(settings.ISSUE_PROXIMITY_LIMIT),
            )
        return [_poi_context(poi) for poi in attached]

    def get_age_seconds(self, issue: Issue) -> int:
        """Seconds since `openedAt` — FR-19's "severe-but-old" signal, precomputed for the client.

        ⚠️ **One `now` for the whole page, taken from `context`.** Calling `timezone.now()` per row
        would let two Issues opened in the same instant report ages a few milliseconds apart, and an
        `?sort=age` page would render subtly non-monotonic — the one thing this field exists to show.
        The fallback keeps a context-less render working rather than raising.
        """
        now = self.context.get("now") or timezone.now()
        return int((now - issue.opened_at).total_seconds())


class IssueListQuerySerializer(CamelCaseSerializer):
    """`GET /issues` query parameters (API §6.5, §4.4) — T7.2's filter and sort allowlists.

    Validation only: every field is a *query* param, so nothing here is written and the serializer is
    never saved. `to_point()` and the validated `bbox` hand typed values to `list_issues()`.

    ⚠️ **An unknown or misspelled param is a `400`, never silently ignored.** `?statuss=resolved`
    returns the unfiltered queue, which looks like a working request with a surprising amount of data
    — the failure mode §4.4 answers with "unknown params → 400".
    """

    category = serializers.CharField(required=False)
    severity = serializers.CharField(required=False)
    status = serializers.CharField(required=False)
    # ⚠️ **`me` is the only accepted value, and that is a disclosure decision.** `?assignedTo=<user
    # id>` would let any caller — this list is public — enumerate a named Authority's workload one id
    # at a time. §6.5 documents only `me`; a cross-authority view is an Admin feature that needs its
    # own task and its own authorization, not a free-text field here.
    assigned_to = serializers.ChoiceField(choices=["me"], required=False)
    bbox = serializers.CharField(required=False)
    # Degree bounds: `nearLat=200` is a malformed request (`400`), not a search of empty space.
    near_lng = serializers.FloatField(required=False, min_value=-180.0, max_value=180.0)
    near_lat = serializers.FloatField(required=False, min_value=-90.0, max_value=90.0)
    # ⚠️ Reuses the Report search bound rather than introducing a second number that can drift from
    # it; `settings/base.py` records that the name now reads narrower than the setting.
    radius_m = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=float(settings.REPORT_SEARCH_MAX_RADIUS_M),
    )
    opened_after = serializers.DateTimeField(required=False)
    # `allow_blank=False`: `?q=` with nothing after it is a client bug, and treating it as "no search"
    # would hide a broken search box that silently returns the whole queue.
    q = serializers.CharField(required=False, allow_blank=False)
    sort = serializers.ChoiceField(choices=list(SORT_CHOICES), required=False)

    # ⚠️ The paginator's params. `reject_unknown_fields()` only knows this serializer's fields, so
    # omitting them makes `?cursor=…` — every page after the first — a `400`, while the first page
    # keeps working; a test that never turns a page would not catch it.
    PAGINATION_PARAMS = ("limit", "cursor")

    def validate_category(self, value: str) -> list[str]:
        """`?category=roads,water_drainage` → slugs, checked against the taxonomy (C-2).

        Retired slugs are accepted, for the reason `ReportListQuerySerializer` records: an Issue
        classified before a retirement still points at that category and must stay findable.
        """
        slugs = set(Category.objects.values_list("slug", flat=True))
        return allowlisted_csv(value, allowed=slugs, label="category")

    def validate_severity(self, value: str) -> list[str]:
        """`?severity=high,medium` → the four bands (C-1, Q2 resolved)."""
        return allowlisted_csv(value, allowed=set(SeveritySignal.values), label="severity")

    def validate_status(self, value: str) -> list[str]:
        """`?status=in_progress,resolved` — the workflow statuses, minus the moderated pair.

        ⚠️ **`hidden`/`removed` are not selectable, and the message says so rather than "unknown
        value".** They are real members of `IssueStatus`, so "unknown" would be a lie that sends a
        developer looking for a typo. Refusing them here is presentation; the actual suppression is
        unconditional in `list_issues()`, because a filter allowlist protects only the HTTP path.
        """
        selectable = set(IssueStatus.values) - set(MODERATED_ISSUE_STATUSES)
        submitted = [item.strip() for item in value.split(",") if item.strip()]
        if moderated := sorted(set(submitted) & set(MODERATED_ISSUE_STATUSES)):
            raise serializers.ValidationError(
                f"Moderated Issues cannot be listed: {', '.join(moderated)}.", code="INVALID"
            )
        return allowlisted_csv(value, allowed=selectable, label="status")

    def validate_bbox(self, value: str) -> Polygon:
        """`?bbox=minLng,minLat,maxLng,maxLat` → a WGS84 rectangle (§4.4).

        ⚠️ **`srid` is set explicitly.** `Polygon.from_bbox()` produces an SRID-less geometry, and
        comparing that against a `geography(4326)` column either errors or is silently assumed to
        match — the kind of assumption that holds until the day it does not.

        ⚠️ **A box whose min is not strictly below its max is refused, not normalized.** Swapping the
        pair "helpfully" would accept an antimeridian-crossing box as its own complement — a client
        asking for a sliver near 180° would receive the rest of the planet. Single-city deployment
        makes such a box a bug in every case (BR-35), and a degenerate zero-area box is one too.
        """
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise serializers.ValidationError(
                "Expected four comma-separated numbers: minLng,minLat,maxLng,maxLat.",
                code="INVALID",
            )
        try:
            min_lng, min_lat, max_lng, max_lat = (float(part) for part in parts)
        except ValueError as exc:
            raise serializers.ValidationError(
                "Expected four comma-separated numbers: minLng,minLat,maxLng,maxLat.",
                code="INVALID",
            ) from exc

        if not (-180.0 <= min_lng <= 180.0 and -180.0 <= max_lng <= 180.0):
            raise serializers.ValidationError(
                "Longitude values must be between -180 and 180.", code="INVALID"
            )
        if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
            raise serializers.ValidationError(
                "Latitude values must be between -90 and 90.", code="INVALID"
            )
        if min_lng >= max_lng or min_lat >= max_lat:
            raise serializers.ValidationError(
                "minLng must be less than maxLng, and minLat less than maxLat.", code="INVALID"
            )

        box = Polygon.from_bbox((min_lng, min_lat, max_lng, max_lat))
        box.srid = 4326
        return box

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Unknown params, the two spatial forms, the all-or-nothing triple, and `assignedTo=me`."""
        reject_unknown_fields(
            self,
            extra_allowed=self.PAGINATION_PARAMS,
            message="This query parameter is not accepted by this endpoint.",
        )

        spatial = {"nearLng": "near_lng", "nearLat": "near_lat", "radiusM": "radius_m"}
        present = {sent: attr for sent, attr in spatial.items() if attrs.get(attr) is not None}

        # ⚠️ **Checked before the triple, and the order is what makes the message useful.** A request
        # sending `bbox` *and* a partial triple would otherwise be told to complete the triple — which
        # is not the fix. §4.4 offers the two spatial forms as alternatives ("or"), and applying both
        # would intersect them: a caller who believes they widened a search would have narrowed it.
        if attrs.get("bbox") is not None and present:
            raise serializers.ValidationError(
                {"bbox": "Use bbox or nearLng/nearLat/radiusM, not both."}, code="INVALID"
            )

        # ⚠️ **All three or none — a partial triple is refused, never defaulted**, for the reason
        # `ReportListQuerySerializer` records: a default radius invents a policy number the spec does
        # not have, and ignoring a lone `nearLng` returns the whole city to a client that asked for
        # one street corner. Duplicated rather than shared: that serializer's error shape is asserted
        # by the T2.7 suite, and the two param sets are free to diverge as each endpoint's spec does.
        if present and len(present) != len(spatial):
            missing = sorted(set(spatial) - set(present))
            raise serializers.ValidationError(
                dict.fromkeys(missing, "Required together with nearLng, nearLat and radiusM."),
                code="REQUIRED",
            )

        # ⚠️ **`assignedTo=me` without a session is a `400`, not an empty page.** "You have no work
        # assigned" and "you are not signed in" are the same JSON otherwise, and the first reading is
        # the one an operator whose session just expired will believe.
        if attrs.get("assigned_to") == "me":
            request = self.context.get("request")
            if request is None or not request.user.is_authenticated:
                raise serializers.ValidationError(
                    {"assigned_to": "Sign in to filter by your own assignments."},
                    code="INVALID",
                )

        return attrs

    def to_point(self) -> Point | None:
        """The search centre, or `None` when this request is not a radius search.

        Reuses `LocationSerializer.to_point()` so the `(lng, lat)` argument order stays defined in
        exactly one place — the transposition that would make every real query read as out-of-city.
        """
        data = self.validated_data
        if data.get("near_lng") is None:
            return None
        return LocationSerializer.to_point({"lng": data["near_lng"], "lat": data["near_lat"]})


class IssueMapQuerySerializer(CamelCaseSerializer):
    """Validated query contract for the public GeoJSON issue map (API section 6.9)."""

    category = serializers.CharField(required=False)
    severity = serializers.CharField(required=False)
    status = serializers.CharField(required=False)
    bbox = serializers.CharField(required=True)
    zoom = serializers.IntegerField(required=False, default=12, min_value=0, max_value=22)

    validate_category = IssueListQuerySerializer.validate_category
    validate_severity = IssueListQuerySerializer.validate_severity
    validate_status = IssueListQuerySerializer.validate_status
    validate_bbox = IssueListQuerySerializer.validate_bbox

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(
            self,
            message="This query parameter is not accepted by this endpoint.",
        )
        return attrs


class AnalyticsSummaryQuerySerializer(CamelCaseSerializer):
    """Query parameters for the Authority/Admin analytics summary (API section 6.9)."""

    from_date = serializers.DateTimeField(required=False)
    to_date = serializers.DateTimeField(required=False)
    group_by = serializers.ChoiceField(
        choices=["category", "severity", "status", "area"], default="category"
    )
    category = serializers.CharField(required=False)
    bbox = serializers.CharField(required=False)

    validate_category = IssueListQuerySerializer.validate_category
    validate_bbox = IssueListQuerySerializer.validate_bbox

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_fields(
            self,
            message="This query parameter is not accepted by this endpoint.",
        )
        if (
            attrs.get("from_date")
            and attrs.get("to_date")
            and attrs["from_date"] > attrs["to_date"]
        ):
            raise serializers.ValidationError(
                {"from_date": "from must be earlier than to."}, code="INVALID"
            )
        return attrs
