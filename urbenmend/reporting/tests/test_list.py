"""`GET /reports` — the role-scoped collection read (T2.7, API §6.3).

Three things are under test, in order of how badly they fail when wrong:

1. **Visibility.** §6.3 fixes "Citizen: own reports · Authority: reports in scope · Admin: all".
   A widened queryset here publishes every report in the city to any account with a session, and
   nothing in the response shape would look different.
2. **Filter strictness.** api-conventions.md fixes `400` for an unknown query param. Silently
   dropping `?statuss=triaged` answers `200` with the *unfiltered* list — a citizen looking for
   their open reports is shown all of them with no signal the filter was ignored.
3. **Cursor correctness under `?sort=`.** Cursor paging exists because offsets skip and repeat rows
   (§4.4); a tie-break that does not move direction with the sort reintroduces exactly that bug, and
   only when two reports share a `created_at`. `test_paging_across_a_shared_timestamp_*` are the
   tests that can see it.

[doc: API §6.3, §1.3, §4.4; FR-11, NFR-2, BR-26; api-conventions.md "Query params"]
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.identity.models import Role, User, UserStatus
from urbenmend.identity.services import AuthorizationError
from urbenmend.identity.tests.factories import AdminFactory, AuthorityFactory, UserFactory
from urbenmend.media.models import MediaState
from urbenmend.media.tests.factories import ReadyMediaFactory
from urbenmend.reporting import selectors
from urbenmend.reporting.models import Report, ReportStatus
from urbenmend.reporting.tests.factories import (
    DEFAULT_LOCATION,
    ClassifiedReportFactory,
    ReportFactory,
)

pytestmark = pytest.mark.django_db

# ~10.2 km east of `DEFAULT_LOCATION` (0.1° of longitude at 23.8°N). Far enough that a 5 km search
# excludes it and a 20 km search includes it, so one fixture exercises both sides of `?radiusM=`.
FAR_LOCATION = Point(90.5125, 23.8103, srid=4326)


def _url(**params: Any) -> str:
    url = reverse("api:reports")
    return f"{url}?{urlencode(params)}" if params else url


def _signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _ids(body: dict[str, Any]) -> list[str]:
    return [item["id"] for item in body["data"]]


def _scoped_authority(slug: str = "roads") -> User:
    """An active Authority holding exactly one category.

    ⚠️ Scope is granted through the M2M directly rather than through `set_category_scope()`: that
    service is the subject of the T1.6 suite, and routing a fixture through it would make one
    provisioning bug fail this module for an unrelated reason (the posture `ReportFactory` records).
    """
    authority = AuthorityFactory.create()
    authority.category_scope.add(Category.objects.get(slug=slug))
    return authority


# --------------------------------------------------------------------------------------
# Visibility — §6.3's three rules
# --------------------------------------------------------------------------------------


def test_a_citizen_sees_only_their_own_reports() -> None:
    mine = ReportFactory.create()
    ReportFactory.create()  # somebody else's

    body = _signed_in(mine.author).get(_url()).json()

    assert _ids(body) == [str(mine.pk)]


def test_an_authority_sees_only_reports_inside_their_category_scope() -> None:
    """BR-26. The leak this guards is the whole reason scope is applied in the queryset rather than
    after fetching: a post-filter over a fetched page returns short pages, which leaks how many
    out-of-scope reports exist even when it hides their contents."""
    authority = _scoped_authority("roads")
    in_scope = ClassifiedReportFactory.create()
    out_of_scope = ClassifiedReportFactory.create(
        category=Category.objects.get(slug="water_drainage")
    )

    body = _signed_in(authority).get(_url()).json()

    assert _ids(body) == [str(in_scope.pk)]
    assert str(out_of_scope.pk) not in _ids(body)


def test_an_unclassified_report_is_in_no_authoritys_list() -> None:
    """⚠️ **Correct, and the tempting fix is the bug.**

    A report sits at `category IS NULL` between `POST /reports` and T3.5's triage, so a scope filter
    over `category_id` excludes it from every Authority's list — a report nobody has categorized yet
    belongs to no department. Widening the filter with `Q(category__isnull=True)` would put every
    un-triaged report in *every* Authority's queue simultaneously, which is not "in scope" under any
    reading of BR-26.
    """
    authority = _scoped_authority("roads")
    ReportFactory.create()  # unclassified — the state `POST /reports` leaves behind

    assert _ids(_signed_in(authority).get(_url()).json()) == []


def test_an_unscoped_authority_sees_nothing() -> None:
    """⚠️ **An empty `category_scope` grants nothing** (T1.5). `scoped_category_ids()` returns an
    empty set, and `or set()` in the selector is the fail-closed spelling — reading a falsy scope as
    "unrestricted" would silently promote a freshly provisioned Authority to Admin."""
    ClassifiedReportFactory.create()

    assert _ids(_signed_in(AuthorityFactory.create()).get(_url()).json()) == []


def test_an_admin_sees_every_report_including_unclassified_ones() -> None:
    """⚠️ **Admins bypass scope with no filter at all**, rather than being handed the set of all
    current category ids: an id-set filter would exclude a category added after the request began,
    and would exclude the `category IS NULL` rows entirely — leaving nobody able to see a report
    between submission and triage."""
    unclassified = ReportFactory.create()
    classified = ClassifiedReportFactory.create()

    body = _signed_in(AdminFactory.create()).get(_url()).json()

    assert set(_ids(body)) == {str(unclassified.pk), str(classified.pk)}


def test_a_suspended_authority_is_stopped_at_the_session_layer() -> None:
    """⚠️ **`401`, not `403`, and the reason is worth knowing before someone "fixes" it.**

    `is_active` is derived from `status` (A6), and DRF's `SessionAuthentication.authenticate()`
    returns `None` for an inactive user — so a suspended account's live session stops authenticating
    the instant the status flips, and `IsAuthenticated` denies before any view code runs. The
    revocation is immediate, which is what sessions-over-JWT was chosen for (Arch §8); it simply
    lands on the authentication layer rather than the authorization one.

    ⚠️ **That does not make the selector's `require_role()` redundant** — see the test below. The
    HTTP path gets two independent refusals; a management command or the worker gets only the
    service-layer one, which is the enforcement point (FR-3).
    """
    authority = _scoped_authority()
    ClassifiedReportFactory.create()
    # Signed in *before* the suspension, so the session is live and only the status change denies.
    client = _signed_in(authority)
    authority.status = UserStatus.SUSPENDED
    authority.save(update_fields=["status"])

    response = client.get(_url())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_the_selector_refuses_a_suspended_authority_directly() -> None:
    """⚠️ The FR-3 claim, asserted where it is actually made.

    `has_role()` reads `status` as well as `role` (T1.5), so an account that still has
    `role == "authority"` is refused. Asserted against the selector rather than through HTTP because
    the endpoint's `401` above comes from DRF and would keep passing if this check were deleted.
    """
    authority = _scoped_authority()
    authority.status = UserStatus.SUSPENDED
    authority.save(update_fields=["status"])

    assert authority.role == Role.AUTHORITY
    with pytest.raises(AuthorizationError) as caught:
        selectors.list_reports(actor=authority)

    # ⚠️ The denial names neither the role nor the resource (T1.5) — "Authority role required"
    # tells an attacker which capability tier to go after, and repeated across endpoints it maps
    # the permission matrix from the outside.
    message = str(caught.value).lower()
    assert "authority" not in message
    assert "report" not in message


def test_an_anonymous_caller_is_401_not_403() -> None:
    """⚠️ **The collection is not public even though `GET /reports/{id}` is.** There is no such
    thing as "all reports" without a caller — §6.3 scopes the list by role — so an anonymous list
    would have to mean "every report in the city", a different endpoint nobody specified.

    `401`, not DRF's default `403`: §4.2 fixes it, and the rewrite is done once globally (T1.3).
    """
    ReportFactory.create()

    response = Client().get(_url())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("status", [ReportStatus.HIDDEN, ReportStatus.REMOVED])
def test_a_moderated_report_is_excluded_for_its_own_author(status: str) -> None:
    """⚠️ **Excluded for every role, Admin included, and the cost is stated rather than hidden.**

    Rendering a hidden row inside a list republishes exactly the content FR-31 suppressed, and a
    list request carries no id to answer `410` about. The consequence is that a citizen whose report
    was moderated sees it vanish from their own list with no explanation — surfacing it to the
    author alone would leak the moderation decision into a public-shaped payload, and the review
    surface for that is §6.13's, not this one.
    """
    report = ReportFactory.create(status=status)

    assert _ids(_signed_in(report.author).get(_url()).json()) == []


def test_a_moderated_report_is_excluded_for_an_admin_too() -> None:
    ReportFactory.create(status=ReportStatus.HIDDEN)
    visible = ReportFactory.create()

    body = _signed_in(AdminFactory.create()).get(_url()).json()

    assert _ids(body) == [str(visible.pk)]


# --------------------------------------------------------------------------------------
# The §1.3 envelope, and the item shape
# --------------------------------------------------------------------------------------


def test_the_response_is_the_data_page_meta_envelope() -> None:
    """§1.3. DRF's `CursorPagination` emits `{next, previous, results}`, so this is the one
    assertion that catches `StandardCursorPagination` being bypassed."""
    report = ReportFactory.create()

    body = _signed_in(report.author).get(_url()).json()

    assert set(body) == {"data", "page", "meta"}
    assert set(body["page"]) == {"nextCursor", "prevCursor", "limit"}
    assert body["page"]["limit"] == 20
    assert body["meta"] == {"count": 1}


def test_next_cursor_is_a_bare_token_not_a_url() -> None:
    """§1.2/§4.4: `nextCursor` is an opaque token the client passes back as `?cursor=`. DRF's own
    next-link is an absolute URL, which would leak the internal host and force the client to parse
    a query string in order to page."""
    author = UserFactory.create()
    ReportFactory.create_batch(3, author=author)

    body = _signed_in(author).get(_url(limit=2)).json()

    cursor = body["page"]["nextCursor"]
    assert cursor
    assert not cursor.startswith("http")
    assert "://" not in cursor


def test_a_list_item_carries_the_same_shape_as_the_detail_body() -> None:
    """One serializer for both, so a field added to the detail read cannot go missing from the list.

    ⚠️ `media[]` is included on list items, which is what makes the prefetch load-bearing rather
    than an optimization (see `test_one_query_serves_the_whole_pages_media`).
    """
    report = ReportFactory.create()
    media = ReadyMediaFactory.create(report=report, owner=report.author)

    item = _signed_in(report.author).get(_url()).json()["data"][0]

    assert set(item) == {
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
    assert item["media"][0]["id"] == str(media.pk)


def test_media_removed_by_moderation_is_omitted_from_a_list_item() -> None:
    """The same rule the detail read applies, reached through the prefetch queryset rather than a
    second filter — which is the reason `_visible_media()` is shared."""
    report = ReportFactory.create()
    kept = ReadyMediaFactory.create(report=report, owner=report.author)
    ReadyMediaFactory.create(report=report, owner=report.author, state=MediaState.REMOVED)

    item = _signed_in(report.author).get(_url()).json()["data"][0]

    assert [entry["id"] for entry in item["media"]] == [str(kept.pk)]


def test_one_query_serves_the_whole_pages_media() -> None:
    """⚠️ **N+1 guard, asserted as "the count does not grow with the row count".**

    §6.3's list items carry `media[]`, so without `prefetch_related` a 20-item page issues 21
    queries and NFR-2's p95 budget is spent inside a loop no reader of the serializer would notice.
    An exact query number would be brittle (session reads, filter lookups); comparing one row
    against five states the property that actually matters.
    """
    author = UserFactory.create()
    client = _signed_in(author)
    first = ReportFactory.create(author=author)
    ReadyMediaFactory.create(report=first, owner=author)
    # Warm-up: the first request populates the cached session, so its DB read would otherwise be
    # counted only once and make the two measurements differ for the wrong reason.
    assert client.get(_url()).status_code == 200

    with CaptureQueriesContext(connection) as one_row:
        assert client.get(_url()).status_code == 200

    for _ in range(4):
        report = ReportFactory.create(author=author)
        ReadyMediaFactory.create(report=report, owner=author)

    with CaptureQueriesContext(connection) as five_rows:
        response = client.get(_url())
    assert response.json()["meta"]["count"] == 5

    assert len(five_rows) == len(one_row)


# --------------------------------------------------------------------------------------
# Filters — and the `400`s that make them trustworthy
# --------------------------------------------------------------------------------------


def test_status_accepts_a_comma_separated_list() -> None:
    """api-conventions.md: "multiple values comma-separated (`?severity=high,medium`)"."""
    author = UserFactory.create()
    submitted = ReportFactory.create(author=author)
    processing = ReportFactory.create(author=author, status=ReportStatus.PROCESSING)
    ReportFactory.create(author=author, status=ReportStatus.TRIAGED)

    body = _signed_in(author).get(_url(status="submitted,processing")).json()

    assert set(_ids(body)) == {str(submitted.pk), str(processing.pk)}


def test_a_retired_category_is_still_filterable() -> None:
    """⚠️ **Unlike on submission, where a retired slug is refused `400`.** A retired node accepts no
    *new* classification (T0.10), but reports classified before the retirement keep pointing at it —
    filtering them out here would make them unreachable through the documented query, which is
    precisely the historical reference a retire-instead-of-delete lifecycle exists to preserve."""
    author = UserFactory.create()
    retired = Category.objects.get(slug="water_drainage")
    retired.status = CategoryStatus.RETIRED
    retired.save(update_fields=["status"])
    kept = ClassifiedReportFactory.create(author=author, category=retired)
    ClassifiedReportFactory.create(author=author)

    body = _signed_in(author).get(_url(category="water_drainage")).json()

    assert _ids(body) == [str(kept.pk)]


def test_q_matches_the_description_and_the_address() -> None:
    """`address` is searched because "Mirpur Road" is a query a citizen will type and it is not in
    the description."""
    author = UserFactory.create()
    by_description = ReportFactory.create(
        author=author, description="Streetlight out near the bus."
    )
    by_address = ReportFactory.create(author=author, address="Mirpur Road, Dhanmondi")
    ReportFactory.create(author=author, description="Open manhole on the footpath.")

    body = _signed_in(author).get(_url(q="Mirpur")).json()
    assert _ids(body) == [str(by_address.pk)]

    body = _signed_in(author).get(_url(q="streetlight")).json()
    assert _ids(body) == [str(by_description.pk)]


def test_an_unknown_query_param_is_400_rather_than_an_unfiltered_page() -> None:
    """⚠️ **The whole reason the query string goes through a serializer.** Reading
    `request.query_params.get("status")` directly would ignore the typo and answer `200` with every
    report the caller can see — a citizen filtering for their open reports would be shown all of
    them, with nothing in the response saying the filter was dropped."""
    report = ReportFactory.create()

    response = _signed_in(report.author).get(_url(statuss="submitted"))

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert [detail["field"] for detail in body["error"]["details"]] == ["statuss"]


def test_an_unknown_status_value_is_400_not_an_empty_page() -> None:
    """An empty page reads as "you have no matching reports", which for a misspelled value is a
    wrong answer rather than a rejected request."""
    report = ReportFactory.create()

    response = _signed_in(report.author).get(_url(status="triage"))

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "status"


def test_an_unknown_category_value_is_400() -> None:
    report = ReportFactory.create()

    response = _signed_in(report.author).get(_url(category="potholes"))

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "category"


def test_an_empty_filter_value_is_400() -> None:
    """`?status=` is a client bug, not a request for everything: read as "no filter" it would make a
    broken query-string builder look like a working one."""
    report = ReportFactory.create()

    assert _signed_in(report.author).get(_url(status="")).status_code == 400


# --------------------------------------------------------------------------------------
# Spatial search
# --------------------------------------------------------------------------------------


def test_near_and_radius_filter_by_metres() -> None:
    """⚠️ **Metres, because `location` is `geography=True`** (T2.1). On a `geometry(4326)` column the
    same `__dwithin` lookup silently reads `radiusM` as *degrees*, and a 500 m search would cover
    half of Asia — a bug that returns too *many* rows, so no assertion on a single fixture sees it.
    """
    author = UserFactory.create()
    near = ReportFactory.create(author=author)
    far = ReportFactory.create(author=author, location=FAR_LOCATION)
    client = _signed_in(author)

    tight = client.get(
        _url(nearLng=DEFAULT_LOCATION.x, nearLat=DEFAULT_LOCATION.y, radiusM=5000)
    ).json()
    assert _ids(tight) == [str(near.pk)]

    wide = client.get(
        _url(nearLng=DEFAULT_LOCATION.x, nearLat=DEFAULT_LOCATION.y, radiusM=20000)
    ).json()
    assert set(_ids(wide)) == {str(near.pk), str(far.pk)}


def test_the_spatial_triple_is_all_or_nothing() -> None:
    """⚠️ **A centre with no radius must not be ignored.** Dropped, the endpoint answers `200` with
    an unfiltered list while the client believes it asked for a neighbourhood — the same silent
    class of failure as an unknown param, arrived at through a *known* one."""
    report = ReportFactory.create()
    client = _signed_in(report.author)

    response = client.get(_url(nearLng=DEFAULT_LOCATION.x, nearLat=DEFAULT_LOCATION.y))

    assert response.status_code == 400
    assert [detail["field"] for detail in response.json()["error"]["details"]] == ["radiusM"]


def test_a_radius_without_a_centre_is_400() -> None:
    report = ReportFactory.create()

    response = _signed_in(report.author).get(_url(radiusM=1000))

    assert response.status_code == 400
    fields = {detail["field"] for detail in response.json()["error"]["details"]}
    assert fields == {"nearLng", "nearLat"}


def test_a_radius_over_the_cap_is_400() -> None:
    """⚠️ **`REPORT_SEARCH_MAX_RADIUS_M` is our policy, not spec-derived**, and it is a DoS bound
    rather than a preference: an uncapped `radiusM` makes `ST_DWithin` match every row in the table
    on an endpoint any account with a session can reach, defeating the GiST index the geometry
    column carries."""
    report = ReportFactory.create()

    response = _signed_in(report.author).get(
        _url(
            nearLng=DEFAULT_LOCATION.x,
            nearLat=DEFAULT_LOCATION.y,
            radiusM=settings.REPORT_SEARCH_MAX_RADIUS_M + 1,
        )
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "radiusM"


def test_an_out_of_range_latitude_is_400() -> None:
    """Degree bounds, so `nearLat=200` is a malformed request rather than a search that quietly
    matches nothing."""
    report = ReportFactory.create()

    response = _signed_in(report.author).get(
        _url(nearLng=DEFAULT_LOCATION.x, nearLat=200, radiusM=1000)
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "nearLat"


# --------------------------------------------------------------------------------------
# Sorting and cursor paging
# --------------------------------------------------------------------------------------


def _dated_reports(author: User, count: int = 3) -> list[Report]:
    """`count` reports one minute apart, oldest first in the returned list."""
    base = timezone.now() - timedelta(hours=1)
    return [
        ReportFactory.create(author=author, created_at=base + timedelta(minutes=index))
        for index in range(count)
    ]


def test_the_default_order_is_newest_first() -> None:
    """api-conventions.md fixes `-createdAt` as the reports default."""
    author = UserFactory.create()
    oldest, middle, newest = _dated_reports(author)

    body = _signed_in(author).get(_url()).json()

    assert _ids(body) == [str(newest.pk), str(middle.pk), str(oldest.pk)]


def test_sort_created_at_flips_the_order() -> None:
    author = UserFactory.create()
    oldest, middle, newest = _dated_reports(author)

    body = _signed_in(author).get(_url(sort="createdAt")).json()

    assert _ids(body) == [str(oldest.pk), str(middle.pk), str(newest.pk)]


def test_an_unknown_sort_value_is_400() -> None:
    """`?sort=` is checked against a documented allowlist (api-conventions.md). Unvalidated, the
    string would reach the ORM's `order_by` and a `?sort=password` would be a `500` — or worse, an
    ordering over a column no client should be able to sort by."""
    report = ReportFactory.create()

    response = _signed_in(report.author).get(_url(sort="severity"))

    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "sort"


def _page_through(client: Client, **params: Any) -> list[str]:
    """Follow `nextCursor` to exhaustion, returning every id in the order it was served."""
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # A bound, so a broken cursor loops finitely rather than hanging the suite.
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        body = client.get(_url(**query)).json()
        seen.extend(_ids(body))
        cursor = body["page"]["nextCursor"]
        if cursor is None:
            return seen
    raise AssertionError("Cursor paging did not terminate.")


@pytest.mark.parametrize("sort", [None, "createdAt"])
def test_paging_across_a_shared_timestamp_serves_every_row_exactly_once(sort: str | None) -> None:
    """⚠️ **The reason `ReportCursorPagination` exists at all.**

    `created_at` alone is not unique — five reports submitted in the same second share it — and DRF
    builds its cursor comparison from the *ordering tuple*. If the `-pk` tie-break does not move
    direction with `?sort=`, the comparison disagrees with the row order and rows straddling a page
    boundary are skipped or repeated: the exact failure cursor paging was chosen over offsets to
    prevent (§4.4). It is invisible with distinct timestamps and invisible on a single page, so both
    directions are paged here at `limit=2` against a shared timestamp.
    """
    author = UserFactory.create()
    stamp = timezone.now() - timedelta(minutes=5)
    expected = {
        str(report.pk) for report in ReportFactory.create_batch(5, author=author, created_at=stamp)
    }

    params: dict[str, Any] = {"limit": 2}
    if sort is not None:
        params["sort"] = sort
    served = _page_through(_signed_in(author), **params)

    assert len(served) == len(set(served)), "a row was served on two pages"
    assert set(served) == expected


def test_limit_is_clamped_to_the_max_page_size() -> None:
    """⚠️ `page.limit` reports the *clamped* value. Echoing the client's raw `?limit=500` back would
    tell it the server honoured a limit it did not, leaving it to conclude the collection ended."""
    report = ReportFactory.create()

    body = _signed_in(report.author).get(_url(limit=500)).json()

    assert body["page"]["limit"] == 100


def test_the_pagination_params_are_not_rejected_as_unknown_fields() -> None:
    """⚠️ Why `reject_unknown_fields()` takes `extra_allowed`: `?limit=` and `?cursor=` belong to the
    paginator, not to the query serializer, and a serializer that refused them would make every
    second page a `400`."""
    author = UserFactory.create()
    ReportFactory.create_batch(3, author=author)

    assert _signed_in(author).get(_url(limit=2, sort="createdAt")).status_code == 200


def test_the_list_is_scoped_before_the_filters_are_applied() -> None:
    """⚠️ Order matters, and it is observable: a filter appended to `Report.objects.all()` by a
    later caller would leak every report in the city. Starting from the scoped queryset means the
    worst a bad filter can do is return too few rows — so a Citizen filtering by another citizen's
    status still sees only their own."""
    mine = ReportFactory.create(status=ReportStatus.PROCESSING)
    ReportFactory.create(status=ReportStatus.PROCESSING)  # another citizen, same status

    body = _signed_in(mine.author).get(_url(status="processing")).json()

    assert _ids(body) == [str(mine.pk)]


def test_a_citizen_may_not_widen_their_scope_with_a_query_param() -> None:
    """There is no `?author=` — and adding one would be adding an Admin capability to a Citizen
    endpoint. Asserted as a `400` so the absence is deliberate rather than incidental."""
    mine = ReportFactory.create()
    theirs = ReportFactory.create()

    response = _signed_in(mine.author).get(_url(author=str(theirs.author_id)))

    assert response.status_code == 400


def test_role_is_read_from_the_session_not_the_query_string() -> None:
    """A Citizen sending `?role=admin` is refused rather than served the Admin list — the visibility
    branch reads `has_role(actor, ...)` off the session user, and the param is simply unknown."""
    mine = ReportFactory.create()
    ReportFactory.create()

    response = _signed_in(mine.author).get(_url(role=Role.ADMIN))

    assert response.status_code == 400
