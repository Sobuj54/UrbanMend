"""
T2.2 — `POST /reports`: the fast write, and the enqueue that must outlive it.

T2.1 tested the *rules* (`create_report`). This suite tests the two things T2.2 adds, plus the HTTP
contract over the top of them:

1. **The status moves to `processing`,** meaning "a classification job exists" — the one claim only
   this layer can make.
2. **The enqueue is deferred to commit.** This is the whole task. `transaction.on_commit` is not a
   style choice: an inline `.delay()` publishes a message the instant it runs, and a worker can pick
   it up before the transaction commits — then `Report.objects.get(pk=...)` raises `DoesNotExist` and
   the report is never triaged. It is a race, so it fails *intermittently*, under load, in
   production, and never on a developer's machine. The pair of tests around
   `captureOnCommitCallbacks` is what makes the difference observable.

⚠️ **`task_always_eager` is deliberately NOT configured for tests**, and that is load-bearing here.
Eager mode runs the task body synchronously inside the caller's transaction — which means a suite
running eager would pass with the broken inline `.delay()`, because the "worker" would share the
transaction that has not committed and see the row anyway. It converts the exact bug this task
exists to prevent into a green test. `.delay` is patched instead: the assertion is about *when* the
message is published, which is the property that matters.

[doc: plan T2.2; API §6.3; FR-5, FR-12, NFR-3; Arch §4.1; async-worker.md]
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse

from urbenmend.classification.models import Category
from urbenmend.geo.tests.factories import OUTSIDE_POINT
from urbenmend.identity.models import Language
from urbenmend.identity.tests.factories import AuthorityFactory, UserFactory
from urbenmend.reporting import services
from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus
from urbenmend.reporting.tests.factories import DEFAULT_LOCATION

pytestmark = pytest.mark.django_db

# Patched at its use site, not its definition: `submit_report` holds a module-level reference, so
# patching `urbenmend.classification.tasks.classify_report.delay` would leave the service's own
# binding untouched and every assertion below would read zero calls.
DELAY = "urbenmend.reporting.services.classify_report.delay"


def _url() -> str:
    return reverse("api:reports")


def _body(**overrides: Any) -> dict[str, Any]:
    """A minimally valid §6.3 body — central Dhaka, inside the seeded boundary."""
    body: dict[str, Any] = {
        "description": "Large pothole across the lane, two wheels deep.",
        "location": {"lng": DEFAULT_LOCATION.x, "lat": DEFAULT_LOCATION.y},
    }
    body.update(overrides)
    return body


def _signed_in_citizen(**overrides: Any) -> tuple[Client, Any]:
    citizen = UserFactory.create(**overrides)
    client = Client()
    client.force_login(citizen)
    return client, citizen


# ---------------------------------------------------------------------------------------
# submit_report() — the enqueue discipline (Arch §4.1, async-worker.md)
# ---------------------------------------------------------------------------------------


def test_the_task_is_not_published_before_the_transaction_commits() -> None:
    """⚠️ **The deliverable of T2.2, asserted from the failing side.**

    `pytest-django` wraps each test in a transaction it never commits, so a correctly registered
    `on_commit` callback has not run when this returns. An inline `classify_report.delay(...)`
    would already have published — so this test is what turns "someone dropped the `on_commit`
    wrapper" from an intermittent production race into a red test on the first run.
    """
    citizen = UserFactory.create()

    with patch(DELAY) as delay:
        services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    delay.assert_not_called()


def test_the_task_is_published_once_the_transaction_commits() -> None:
    """The other half: deferred, not dropped.

    Without this, `transaction.on_commit(lambda: None)` — or deleting the enqueue outright — would
    satisfy the test above and no report would ever be triaged.
    """
    citizen = UserFactory.create()

    with patch(DELAY) as delay, TestCase.captureOnCommitCallbacks(execute=True):
        report = services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    delay.assert_called_once_with(str(report.pk))


def test_only_the_id_crosses_the_broker_boundary() -> None:
    """⚠️ A `str`, never the `Report` instance and never a `UUID`.

    Passing the instance puts a whole row — author id, free text, coordinates — into Redis (NFR-12)
    and lets the worker act on a pre-commit snapshot rather than re-reading the committed row.
    A `UUID` serializes fine and arrives at the worker as a `str` anyway, so the task's annotation
    would be a lie on the receiving side.
    """
    citizen = UserFactory.create()

    with patch(DELAY) as delay, TestCase.captureOnCommitCallbacks(execute=True):
        services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    (published,) = delay.call_args.args
    assert isinstance(published, str)
    assert Report.objects.filter(pk=published).exists()


def test_a_rolled_back_submission_publishes_nothing() -> None:
    """The guarantee's mirror image: no task ever references a report that does not exist.

    A caller wrapping `submit_report()` in a wider transaction that later fails must not leave a
    message pointing at a row that was rolled away. Django discards `on_commit` callbacks
    registered inside a savepoint when that savepoint rolls back — this asserts we actually rely on
    that rather than publishing beside it.
    """
    citizen = UserFactory.create()

    # The order is the assertion: `captureOnCommitCallbacks` observes from *outside* the savepoint
    # that rolls back, so it would see any callback that survived it.
    with (
        patch(DELAY) as delay,
        TestCase.captureOnCommitCallbacks(execute=True),
        contextlib.suppress(RuntimeError),
        transaction.atomic(),
    ):
        services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )
        raise RuntimeError("the caller's later step failed")

    delay.assert_not_called()
    assert Report.objects.count() == 0


# ---------------------------------------------------------------------------------------
# submit_report() — status and delegation
# ---------------------------------------------------------------------------------------


def test_submission_leaves_the_report_processing_not_submitted() -> None:
    """`processing` means "a classification job exists" — the claim only T2.2 can make.

    `create_report()` writes `submitted` and refuses a `status` argument (T2.1, asserted on its
    signature), so this transition has to happen here or the §6.3 `202` body would be lying.
    """
    citizen = UserFactory.create()

    with patch(DELAY):
        report = services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    report.refresh_from_db()
    assert report.status == ReportStatus.PROCESSING


def test_submission_leaves_the_report_unclassified() -> None:
    """BR-9 — the report is valid, and triage has not run. `classification.state` says `pending`."""
    citizen = UserFactory.create()

    with patch(DELAY):
        report = services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    assert report.is_classified is False
    assert report.severity_signal is None
    assert report.classified_at is None


def test_submission_enforces_every_t21_rule_rather_than_reimplementing_them() -> None:
    """⚠️ Delegation, asserted — the rules must not fork.

    A `submit_report()` that built its own `Report.objects.create()` would pass every happy-path
    test above while quietly skipping the boundary check, BR-3, and the Citizen-only rule. Each of
    these raises only because `create_report()` is on the path.
    """
    with pytest.raises(services.ReportValidationError):
        services.submit_report(
            author=UserFactory.create(), location=DEFAULT_LOCATION, description="short"
        )

    from urbenmend.api.exceptions import OutOfCity

    with pytest.raises(OutOfCity):
        services.submit_report(
            author=UserFactory.create(),
            location=OUTSIDE_POINT,
            description="Large pothole across the lane.",
        )

    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        services.submit_report(
            author=AuthorityFactory.create(),
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )


def test_a_failed_submission_creates_no_row() -> None:
    """`@transaction.atomic` on the outer function — the INSERT and the status UPDATE are one unit.

    Without it, a failure between them would leave a `submitted` report that no job will ever pick
    up: invisible to the worker's queue read and indistinguishable from a pending one.
    """
    with patch(DELAY), pytest.raises(services.ReportValidationError):
        services.submit_report(
            author=UserFactory.create(), location=DEFAULT_LOCATION, description="x"
        )

    assert Report.objects.count() == 0


# ---------------------------------------------------------------------------------------
# POST /reports — the §6.3 contract
# ---------------------------------------------------------------------------------------


def test_a_valid_submission_returns_202_with_the_spec_body() -> None:
    """⚠️ `202`, not `201`: the row is durable, the *resource* is incomplete (§6.3).

    Every key is asserted, in the spec's spelling. `issueId` and `classification` are present and
    empty rather than omitted — a client must be able to read the same shape before and after triage.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        response = client.post(_url(), data=_body(), content_type="application/json")

    assert response.status_code == 202
    payload = response.json()
    assert set(payload) == {"reportId", "status", "issueId", "classification"}
    assert payload["status"] == "processing"
    assert payload["issueId"] is None
    assert payload["classification"] == {"state": "pending"}
    assert Report.objects.filter(pk=payload["reportId"]).exists()


def test_the_response_body_carries_no_snake_case_keys() -> None:
    """The §1 camelCase contract — "the single easiest way for the implementation to silently drift".

    `report_id`/`issue_id` are the accidental output of a serializer that skipped the mixin, and a
    client reading `reportId` would get `KeyError` on every submission.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        response = client.post(_url(), data=_body(), content_type="application/json")

    assert not [key for key in response.json() if "_" in key]


def test_the_enqueue_survives_the_view_layer() -> None:
    """The service defers to commit; this asserts the *request* actually reaches that commit.

    ⚠️ `ATOMIC_REQUESTS` is not enabled, so the view's transaction is the service's own. If that
    ever changes, an outer request-level transaction becomes the commit point — this test is what
    notices, instead of reports silently never being triaged in production.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY) as delay, TestCase.captureOnCommitCallbacks(execute=True):
        response = client.post(_url(), data=_body(), content_type="application/json")

    delay.assert_called_once_with(response.json()["reportId"])


def test_an_unauthenticated_submission_is_401() -> None:
    """FR-3 / Q4 resolved — anonymous write access is not supported.

    `401`, not `403`: the T1.3 fix to DRF's `NotAuthenticated` rewrite has to hold on new endpoints
    too, and it holds globally rather than per view.
    """
    response = Client().post(_url(), data=_body(), content_type="application/json")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_an_authority_submission_is_403_and_writes_nothing() -> None:
    """Citizen-only (§6.3), enforced in the service (FR-3) — the view declares no role class."""
    authority = AuthorityFactory.create()
    client = Client()
    client.force_login(authority)

    response = client.post(_url(), data=_body(), content_type="application/json")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert Report.objects.count() == 0


def test_a_submission_without_a_csrf_token_is_403() -> None:
    """⚠️ The T1.4 note, applied: a new authenticated endpoint must not silently lose CSRF.

    This view keeps the default `authentication_classes`, so `SessionAuthentication` enforces CSRF.
    The failure mode T1.4 recorded is a future view setting `authentication_classes = []` and
    dropping enforcement with no warning — asserted here per endpoint, not assumed once globally.
    """
    client, _ = _signed_in_citizen()
    client.cookies.pop(settings.CSRF_COOKIE_NAME, None)
    client.handler.enforce_csrf_checks = True

    response = client.post(_url(), data=_body(), content_type="application/json")

    assert response.status_code == 403
    assert "CSRF" in response.json()["error"]["message"]


def test_an_out_of_city_location_is_422_out_of_city() -> None:
    """C-11 — and the code, not just the status, since `422` is shared with other rule violations."""
    client, _ = _signed_in_citizen()
    body = _body(location={"lng": OUTSIDE_POINT.x, "lat": OUTSIDE_POINT.y})

    response = client.post(_url(), data=body, content_type="application/json")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OUT_OF_CITY"


def test_an_inadequate_description_is_400_validation_failed() -> None:
    """BR-3 with no media — `400`, distinct from the `422` above (§4.2, the T2.1 distinction)."""
    client, _ = _signed_in_citizen()

    response = client.post(
        _url(), data=_body(description="pothole"), content_type="application/json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_a_missing_location_names_the_field() -> None:
    """§4.1 `details[].field` — the citizen has to be told *which* input to fix.

    This is why the serializer duplicates a check the service also makes: the service's message is
    generic, and a field-level answer is the more useful one.
    """
    client, _ = _signed_in_citizen()
    body = _body()
    del body["location"]

    response = client.post(_url(), data=body, content_type="application/json")

    assert response.status_code == 400
    assert "location" in {detail["field"] for detail in response.json()["error"]["details"]}


def test_an_impossible_latitude_is_400_not_422_out_of_city() -> None:
    """⚠️ A transposed lat/lng must not come back as "we do not serve your city".

    `lat: 200` is not a coordinate; it is a malformed body. Without the range bounds it builds a
    valid GEOS point, misses the boundary polygon, and returns `422 OUT_OF_CITY` — sending a client
    with swapped arguments to debug the wrong thing entirely.
    """
    client, _ = _signed_in_citizen()
    body = _body(location={"lng": 23.8103, "lat": 200.0})

    response = client.post(_url(), data=body, content_type="application/json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_derived_fields_are_rejected_not_silently_dropped() -> None:
    """⚠️ `POST {"severitySignal": "critical"}` must not answer `202`.

    DRF's default drops unknown keys, so the wrong implementation returns a success body and the
    citizen believes they filed a Critical report. Severity is derived and read-only to every client
    (api-conventions.md); an Authority override is the only human input, and not here.
    """
    client, _ = _signed_in_citizen()

    response = client.post(
        _url(),
        data=_body(severitySignal="critical", status="triaged"),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert Report.objects.count() == 0


def test_media_ids_are_refused_while_upload_is_unbuilt() -> None:
    """⚠️ Refused, not ignored (T2.4/T2.6 own media).

    Dropping the key would let a photo-only submission fail BR-3 with "describe the problem" — an
    error about `description` when the client did attach a photo, and no way to act on it.
    """
    client, _ = _signed_in_citizen()
    body = _body(description="", mediaIds=["3f2a6f3e-0000-4000-8000-000000000000"])

    response = client.post(_url(), data=body, content_type="application/json")

    assert response.status_code == 400
    assert "mediaIds" in {detail["field"] for detail in response.json()["error"]["details"]}


def test_an_empty_media_ids_list_is_accepted() -> None:
    """A client that always sends the key is not punished for it — only a non-empty value is refused."""
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        response = client.post(_url(), data=_body(mediaIds=[]), content_type="application/json")

    assert response.status_code == 202


def test_a_category_hint_is_recorded_as_a_citizen_hint() -> None:
    """§6.3's `category` is a *hint*, kept with its provenance (T2.1) — not a classification."""
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        response = client.post(
            _url(), data=_body(category="roads"), content_type="application/json"
        )

    assert response.status_code == 202
    report = Report.objects.get(pk=response.json()["reportId"])
    assert report.category is not None
    assert report.category.slug == "roads"
    assert report.classification_source == ClassificationSource.CITIZEN
    # ⚠️ Still unclassified: the hint fills `category`, and `is_classified` keys on `classified_at`.
    # Keying it on `category` would make T3.5's worker skip every hinted report, forever, silently.
    assert report.is_classified is False
    assert response.json()["classification"] == {"state": "pending"}


def test_an_unknown_category_hint_is_rejected_not_coerced_to_other() -> None:
    """BR-7's coercion is for *LLM* output. A human on an unknown slug is running a stale client.

    ⚠️ **`400`, not `422`** — and the distinction is the one T2.1 built two exception types for.
    §6.3 lists "category (if given) must be in taxonomy (C-2)" among its validation rules and names
    `422` for `OUT_OF_CITY` alone; a value outside a controlled set is the same class of error as
    `preferredLanguage: "fr"` on `PATCH /users/me`, which is `400`. Reserving `422` for genuine
    business-rule violations is what keeps `OUT_OF_CITY` meaningful to a client.
    """
    client, _ = _signed_in_citizen()

    response = client.post(
        _url(), data=_body(category="teleportation"), content_type="application/json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert Report.objects.count() == 0


def test_the_language_falls_back_to_the_authors_preference() -> None:
    """FR-12 — not the column default `"en"`.

    A Bangla-preferring citizen whose client omits `language` must not have their report handled in
    English: the same value reaches T3.5's prompt and BR-30's notification copy.
    """
    client, _ = _signed_in_citizen(preferred_language=Language.BANGLA)

    with patch(DELAY):
        response = client.post(_url(), data=_body(), content_type="application/json")

    assert Report.objects.get(pk=response.json()["reportId"]).language == "bn"


def test_an_explicit_language_wins_over_the_preference() -> None:
    """The fallback is a default, not an override — a bilingual citizen can file in either language."""
    client, _ = _signed_in_citizen(preferred_language=Language.BANGLA)

    with patch(DELAY):
        response = client.post(_url(), data=_body(language="en"), content_type="application/json")

    assert Report.objects.get(pk=response.json()["reportId"]).language == "en"


def test_the_author_comes_from_the_session_not_the_body() -> None:
    """⚠️ There is no `author` field, and there must not be one.

    Accepting one would let any citizen file reports under another account — attributing a false
    report to a real person and poisoning the corroboration count an Issue is built from (FR-16).
    Rejected as an unknown field rather than ignored, so the attempt is visible.
    """
    client, citizen = _signed_in_citizen()
    victim = UserFactory.create()

    response = client.post(
        _url(), data=_body(author=str(victim.pk)), content_type="application/json"
    )

    assert response.status_code == 400

    with patch(DELAY):
        ok = client.post(_url(), data=_body(), content_type="application/json")

    assert Report.objects.get(pk=ok.json()["reportId"]).author_id == citizen.pk


def test_a_registered_but_unverified_citizen_may_submit() -> None:
    """BR-30 gates *notification* on verification, not intake (T2.1).

    The unverified capability set is explicitly unspecified — narrowing it here would invent a rule,
    and would silently block every citizen who has not yet clicked through a channel that ❓Q5 means
    nothing can currently send.
    """
    from urbenmend.identity.tests.factories import RegisteredUserFactory

    client = Client()
    client.force_login(RegisteredUserFactory.create())

    with patch(DELAY):
        response = client.post(_url(), data=_body(), content_type="application/json")

    assert response.status_code == 202


def test_get_reports_is_405_until_t27() -> None:
    """The route exists for `POST` only. `405` is the honest answer; `404` would deny the resource."""
    client, _ = _signed_in_citizen()

    assert client.get(_url()).status_code == 405


# ---------------------------------------------------------------------------------------
# T2.3's target (BR-5)
# ---------------------------------------------------------------------------------------
@pytest.mark.xfail(
    strict=True,
    reason="T2.3 owns Idempotency-Key. Deliberately failing so the requirement has a target.",
)
def test_a_replayed_idempotency_key_returns_the_original_report() -> None:
    """⚠️ **Failing on purpose** — BR-5, API §6.3 `Idempotency-Key`, plan T2.3.

    Today the header is accepted and ignored, so a retried submission on a flaky mobile connection
    creates a second Report — which then clusters into the same Issue and inflates the corroboration
    count that FR-16 uses to judge how many people are affected. One person's dropped connection
    reads as two complaints.

    `strict=True` per the A10 precedent: when T2.3 lands, this test *passing* fails the suite until
    the marker is deleted, so the fix cannot ship with a stale `xfail` hiding whether it worked.
    """
    client, _ = _signed_in_citizen()
    # `headers=`, not the legacy `HTTP_*` WSGI-environ spelling: the header name is part of the §6.3
    # contract, so it is written here exactly as a client sends it.
    headers = {"Idempotency-Key": "3f2a6f3e-1111-4000-8000-000000000000"}

    with patch(DELAY):
        first = client.post(_url(), data=_body(), content_type="application/json", headers=headers)
        second = client.post(_url(), data=_body(), content_type="application/json", headers=headers)

    assert second.json()["reportId"] == first.json()["reportId"]
    assert Report.objects.count() == 1


def test_the_seeded_taxonomy_is_present_for_this_suite() -> None:
    """A guard, not a feature test: `category="roads"` above depends on the migration's seed.

    Without this, a broken taxonomy seed would surface as a confusing `422` on the hint test rather
    than as "the taxonomy is missing".
    """
    assert Category.objects.filter(slug="roads", status="active").exists()
