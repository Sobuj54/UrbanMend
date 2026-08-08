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
from django.utils import timezone

from urbenmend.classification.models import Category
from urbenmend.geo.tests.factories import OUTSIDE_POINT
from urbenmend.identity.models import Language
from urbenmend.identity.tests.factories import AuthorityFactory, UserFactory
from urbenmend.reporting import services
from urbenmend.reporting.models import (
    ClassificationSource,
    Report,
    ReportStatus,
    SeveritySignal,
)
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
        acknowledgement = services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    delay.assert_called_once_with(acknowledgement.report_id)


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

    ⚠️ **Read back from the database, not off the return value.** T2.3 made `submit_report()` return
    a `SubmissionAcknowledgement` rather than the `Report`, and the acknowledgement carries the
    status it *reported* — asserting on that alone would pass even if the UPDATE never reached the
    row. Both are checked: the row moved, and the body agrees with it.
    """
    citizen = UserFactory.create()

    with patch(DELAY):
        acknowledgement = services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    report = Report.objects.get(pk=acknowledgement.report_id)
    assert report.status == ReportStatus.PROCESSING
    assert acknowledgement.status == ReportStatus.PROCESSING


def test_submission_leaves_the_report_unclassified() -> None:
    """BR-9 — the report is valid, and triage has not run. `classification.state` says `pending`."""
    citizen = UserFactory.create()

    with patch(DELAY):
        acknowledgement = services.submit_report(
            author=citizen,
            location=DEFAULT_LOCATION,
            description="Large pothole across the lane.",
        )

    report = Report.objects.get(pk=acknowledgement.report_id)
    assert report.is_classified is False
    assert report.severity_signal is None
    assert report.classified_at is None
    assert acknowledgement.classification == {"state": "pending"}


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
# Idempotency-Key — BR-5, API §4.6, T2.3
#
# ⚠️ **`captureOnCommitCallbacks(execute=True)` is load-bearing in every test below, and its
# placement is the assertion.** `idempotency.complete()` is registered with `transaction.on_commit`
# (a completed record is a promise the row exists), and pytest-django never commits — so *inside* the
# block the key is still in flight, and *after* it the key is completed. That is not a testing
# artifact: it is exactly the production window a double-tap lands in, which is why the in-flight and
# replay cases are written as "inside" and "outside" the same construct.
# ---------------------------------------------------------------------------------------

IDEMPOTENCY_KEY = "3f2a6f3e-1111-4000-8000-000000000000"


def _post(client: Client, key: str | None = IDEMPOTENCY_KEY, **overrides: Any) -> Any:
    """`POST /reports` with an `Idempotency-Key`, in the spelling §6.3 documents.

    `headers=`, not the legacy `HTTP_*` WSGI-environ spelling — the header name is part of the
    contract, and `**{"HTTP_IDEMPOTENCY_KEY": ...}` would also unpack into `Client.post`'s fourth
    positional parameter (`secure: bool`) rather than the environ.
    """
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        _url(), data=_body(**overrides), content_type="application/json", headers=headers
    )


def test_a_replayed_idempotency_key_returns_the_original_report() -> None:
    """**The T2.3 deliverable** — BR-5, API §4.6, §6.3.

    Without this, a retried submission on a flaky mobile connection creates a second Report, which
    clusters into the same Issue and inflates the corroboration count FR-16 uses to judge how many
    people are affected. One person's dropped connection reads as two complaints.

    ⚠️ This test previously carried `xfail(strict=True)` as T2.3's target (the A10 precedent). The
    marker is gone because the behaviour landed — that is what `strict=True` forces.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        with TestCase.captureOnCommitCallbacks(execute=True):
            first = _post(client)
        second = _post(client)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["reportId"] == first.json()["reportId"]
    assert Report.objects.count() == 1


def test_a_replay_returns_the_body_verbatim_not_the_current_state() -> None:
    """§6.3 as amended: a replay "returns this same `202` body verbatim".

    ⚠️ **The point is what happens after triage.** The replayed acknowledgement must still read
    `status: processing` and `classification.state: pending` even once the report has moved on —
    re-serializing the live row would make the POST response a second, weaker `GET /reports/{id}`
    and let a retrying client tell a re-delivered response from a first one by its content.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        with TestCase.captureOnCommitCallbacks(execute=True):
            first = _post(client)

        # Triage lands between the two requests, exactly as it would in production.
        report = Report.objects.get(pk=first.json()["reportId"])
        report.status = ReportStatus.TRIAGED
        report.severity_signal = SeveritySignal.HIGH
        report.classified_at = timezone.now()
        report.classification_source = ClassificationSource.LLM
        report.save()

        second = _post(client)

    assert second.json() == first.json()
    assert second.json()["status"] == "processing"
    assert second.json()["classification"] == {"state": "pending"}


def test_a_replay_carries_the_idempotency_replayed_header() -> None:
    """§4.6 — "the only way a client can tell a re-delivered acknowledgement from a first one".

    ⚠️ Asserted on **both** responses. The bodies are identical by design, so a header that were
    always present, or always absent, would satisfy a one-sided assertion and signal nothing.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        with TestCase.captureOnCommitCallbacks(execute=True):
            first = _post(client)
        second = _post(client)

    assert "Idempotency-Replayed" not in first
    assert second["Idempotency-Replayed"] == "true"


def test_the_replayed_body_carries_no_extra_keys() -> None:
    """The acknowledgement's `replayed` flag must not leak into the §6.3 body.

    It is a transport fact, carried by the header. A `replayed` key in the body would break the
    "verbatim" guarantee the previous tests rest on, and clients do not know the field.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        with TestCase.captureOnCommitCallbacks(execute=True):
            _post(client)
        second = _post(client)

    assert set(second.json()) == {"reportId", "status", "issueId", "classification"}


def test_a_second_request_while_the_first_is_in_flight_is_409_in_progress() -> None:
    """§4.6 — the third outcome, and the one that makes the double-tap safe.

    ⚠️ **This is why `complete()` is deferred to `on_commit`.** Until the first transaction commits
    there is no durable row, so replaying its id would hand a client a `reportId` that may never
    exist. `409 IDEMPOTENCY_IN_PROGRESS` tells them to retry shortly instead — a different remedy
    from `IDEMPOTENCY_KEY_REUSED`, which is why the two codes are distinct.

    Both requests run *inside* the capture block, so the first one's completion callback has not run.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY), TestCase.captureOnCommitCallbacks(execute=True):
        first = _post(client)
        second = _post(client)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert Report.objects.count() == 1


def test_the_same_key_with_a_different_body_is_409_key_reused() -> None:
    """§4.6 — reuse is refused, never replayed.

    ⚠️ **Replaying the first result here would be silent data loss.** The client would get a `202`
    and a `reportId` for a submission the server never recorded — a second, genuinely different
    report that vanishes with no way for the citizen to detect it.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        with TestCase.captureOnCommitCallbacks(execute=True):
            _post(client)
        second = _post(client, description="A different problem entirely, on another street.")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert Report.objects.count() == 1


def test_a_reused_key_is_refused_even_while_the_original_is_in_flight() -> None:
    """⚠️ The fingerprint is checked **before** the state, and the ordering is observable.

    A key bound to different content can never be honoured, whatever the original is doing. Reporting
    it as merely "in progress" would tell the client to retry a request that will be refused forever.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY), TestCase.captureOnCommitCallbacks(execute=True):
        _post(client)
        second = _post(client, description="A different problem entirely, on another street.")

    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_a_normalized_resend_is_a_replay_not_a_reuse() -> None:
    """§4.6 — "payload equality is compared on the server's normalized view", not on raw bytes.

    ⚠️ **This is the difference between a safety net and a fault.** A client that pads its
    description, reorders its JSON keys, or writes `90.3990` for `90.399` is making the same
    submission; a byte-level comparison would answer `409` to an ordinary retry.
    """
    client, _ = _signed_in_citizen()
    padded = _body()["description"] + "  "
    lng, lat = DEFAULT_LOCATION.x, DEFAULT_LOCATION.y

    with patch(DELAY):
        with TestCase.captureOnCommitCallbacks(execute=True):
            first = _post(client)
        second = _post(
            client,
            description=padded,
            location={"lat": float(f"{lat:.10f}"), "lng": float(f"{lng:.10f}")},
        )

    assert second.status_code == 202
    assert second.json()["reportId"] == first.json()["reportId"]


def test_a_key_is_scoped_to_one_user() -> None:
    """§4.6 — "keys are scoped **per user** and per operation".

    ⚠️ **Without this, the second citizen to submit gets the first one's report back** — and their
    own is never filed. Clients generate keys independently, so a collision is ordinary, not an
    attack.
    """
    first_client, _ = _signed_in_citizen()
    second_client, _ = _signed_in_citizen()

    with patch(DELAY), TestCase.captureOnCommitCallbacks(execute=True):
        first = _post(first_client)
        second = _post(second_client)

    assert second.status_code == 202
    assert second.json()["reportId"] != first.json()["reportId"]
    assert Report.objects.count() == 2


def test_a_rejected_submission_does_not_consume_its_key() -> None:
    """§4.6 — "a request that fails validation does not consume its key".

    ⚠️ **The natural client behaviour is the test.** Fix the body, retry with the same key. Without
    the release-on-failure path the retry comes back `409 IDEMPOTENCY_KEY_REUSED` and the citizen is
    stuck until the retention window expires — the safety net becoming the obstacle.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        rejected = _post(client, description="short")
        assert rejected.status_code == 400

        with TestCase.captureOnCommitCallbacks(execute=True):
            accepted = _post(client)

    assert accepted.status_code == 202
    assert Report.objects.count() == 1


def test_an_out_of_city_submission_does_not_consume_its_key() -> None:
    """The same guarantee for the `422` path — the release covers every failure, not just `400`.

    A citizen who moved the pin outside Dhaka by accident retries with a corrected coordinate. That
    is a *different* payload, so the key must be free rather than merely replayable.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY):
        rejected = _post(client, location={"lng": OUTSIDE_POINT.x, "lat": OUTSIDE_POINT.y})
        assert rejected.status_code == 422

        with TestCase.captureOnCommitCallbacks(execute=True):
            accepted = _post(client)

    assert accepted.status_code == 202
    assert Report.objects.count() == 1


def test_two_identical_submissions_without_a_key_both_create_a_report() -> None:
    """§4.6 — the header is optional and absence carries **no** de-duplication guarantee.

    ⚠️ **Inferring a key from the payload would be wrong, not merely out of scope.** Two reports
    about the same pothole from the same spot are two corroborating voices under FR-16, and refusing
    the second would silently discard a citizen's submission. De-duplication is opt-in by design.
    """
    client, _ = _signed_in_citizen()

    with patch(DELAY), TestCase.captureOnCommitCallbacks(execute=True):
        _post(client, key=None)
        _post(client, key=None)

    assert Report.objects.count() == 2


def test_a_blank_key_header_is_treated_as_absent() -> None:
    """§4.6 — "a blank value is treated as absent".

    ⚠️ **Otherwise every client that sends an empty header shares one bucket**, so the second
    citizen to submit is handed the first one's report. Some HTTP stacks emit `Idempotency-Key:` for
    an unset value; absence has to mean absence.
    """
    first_client, _ = _signed_in_citizen()
    second_client, _ = _signed_in_citizen()

    with patch(DELAY), TestCase.captureOnCommitCallbacks(execute=True):
        first = _post(first_client, key="   ")
        second = _post(second_client, key="")

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["reportId"] != second.json()["reportId"]
    assert Report.objects.count() == 2


def test_an_over_long_key_is_400_not_truncated() -> None:
    """§4.6 — over-long keys are rejected, and the bound is configuration.

    ⚠️ **Truncating would alias two distinct keys onto one record**, so the holder of the second key
    would be handed the first one's report: the same bug as an unscoped key, arrived at politely.
    """
    client, _ = _signed_in_citizen()
    limit = settings.IDEMPOTENCY_KEY_MAX_LENGTH

    with patch(DELAY):
        accepted = _post(client, key="k" * limit)
        rejected = _post(client, key="k" * (limit + 1))

    assert accepted.status_code == 202
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "VALIDATION_FAILED"


def test_an_authority_is_403_before_the_key_is_examined() -> None:
    """⚠️ T2.1's observable ordering, extended: authorization runs before idempotency.

    A non-Citizen gets `403` and learns nothing else about the request — including whether their key
    was free, which a `409` would tell them. Reversing the two would make the store a probe.
    """
    authority = AuthorityFactory.create()
    client = Client()
    client.force_login(authority)

    with patch(DELAY):
        first = _post(client)
        second = _post(client)

    assert (first.status_code, second.status_code) == (403, 403)
    assert Report.objects.count() == 0


def test_the_seeded_taxonomy_is_present_for_this_suite() -> None:
    """A guard, not a feature test: `category="roads"` above depends on the migration's seed.

    Without this, a broken taxonomy seed would surface as a confusing `422` on the hint test rather
    than as "the taxonomy is missing".
    """
    assert Category.objects.filter(slug="roads", status="active").exists()
