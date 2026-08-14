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
calls `create_report()`; it is written to be callable without a request, so a management command
or the T2.2 view get the same rules (FR-3).

⚠️ **T2.2 adds `submit_report()` on top, and the split is deliberate.** `create_report()` is the
pure write; `submit_report()` is the write *plus* the async hand-off. Keeping them separate means
T2.1's rules stay testable with no broker, and every caller that must trigger triage goes through
one place instead of remembering to enqueue.

⚠️ **T2.3 resolves `Idempotency-Key` inside `submit_report()`, before the write** (plan T2.3,
API §4.6, BR-5) — not in the view, and not in `create_report()`. Not the view, because a duplicate
that reaches the write is already a duplicate and FR-3 puts rules here; not `create_report()`,
because the thing being de-duplicated is *the submission*, and a caller who wants only the T2.1
write (a fixture, a management command, T2.4's media backfill) must not need a key to get it.

[doc: Arch §3 (FR-5, FR-8, FR-9, FR-11); BR-1..BR-3, BR-35; C-3, C-11]
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import structlog
from django.contrib.gis.geos import Point
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from urbenmend.api import idempotency
from urbenmend.api.exceptions import Conflict, OutOfCity
from urbenmend.classification.models import Category, CategoryStatus
from urbenmend.classification.tasks import classify_report
from urbenmend.geo.selectors import is_within_city
from urbenmend.identity.models import Role, User
from urbenmend.identity.services import has_role, require_category_scope

# ⚠️ **`media` imports `reporting` only under `TYPE_CHECKING`, which is what makes this direction
# safe** — `media/models.py` names the FK as the string `"reporting.Report"` and `media/services.py`
# keeps its `Report` annotation behind the guard. Reversing that (a runtime `from urbenmend.reporting
# ... import` in `media`) would turn this line into an import cycle that only fails once Django loads
# the app registry, i.e. at container start rather than in any single test module.
from urbenmend.media import selectors as media_selectors
from urbenmend.media import services as media_services
from urbenmend.reporting.models import ClassificationSource, Report, ReportStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

logger = structlog.get_logger(__name__)

# ⚠️ **The per-operation half of §4.6's "scoped per user and per operation".** A client that mints
# one key per user action and sends it to several endpoints must not have one endpoint's stored
# response replayed for another; the scope is what makes that impossible. It is a stored cache-key
# component, so **renaming it silently frees every key currently held** — every in-flight submission
# becomes a first use again. Treat it as data, not a label.
IDEMPOTENCY_SCOPE = "reports.submit"

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


@dataclass(frozen=True)
class SubmissionAcknowledgement:
    """What `POST /reports` answered — the acceptance record, not the report's live state (§6.3).

    ⚠️ **This exists so a replay and a first response cannot drift.** §6.3 as amended by T2.3 says a
    replay "returns this same `202` body verbatim". If the fresh path serialized a `Report` and the
    replay path returned a stored dict, the two would be produced by different code and could
    disagree the moment a field is added — the client-visible symptom being a retry that looks like
    a *different* submission. Both paths now render this one object through one serializer, so the
    bodies are identical by construction.

    ⚠️ **It is a snapshot, deliberately frozen in time.** By the time a replay arrives, triage may
    have finished and the report may be `triaged` with an `issueId`. Re-reading the row would answer
    a `202` with post-acceptance state, which is `GET /reports/{id}`'s job — §6.3 directs clients
    there for current state precisely so this response can stay deterministic.

    `replayed` is **not** part of the body: the serializer declares only §6.3's four fields, and the
    view turns this flag into the `Idempotency-Replayed` header instead.
    """

    report_id: str
    status: str
    issue_id: str | None
    classification: dict[str, str]
    replayed: bool = False


def _acknowledge(report: Report) -> SubmissionAcknowledgement:
    """Build the §6.3 acceptance record from the row that was just written.

    ⚠️ **`status` is read off the row, never hardcoded `"processing"`** (the T2.2 decision): a
    literal here would keep reporting success after the transition is moved or removed. `str()`
    because the value is stored in the cache for a replay, and pickling a `ReportStatus` member
    would put a domain enum in Redis for a field the contract defines as a string.

    ⚠️ **`issue_id` is `None` in this acceptance snapshot.** Clustering runs after classification
    in the asynchronous triage worker (T4.5), so at acceptance there is genuinely no Issue to name.
    §6.3 documents the field as populating later, via `GET /reports/{id}`.

    ⚠️ **`classification.state` is derived from the row, not fixed to `"pending"`.** It is always
    pending at acceptance today, but deriving it means a future synchronous-classification path
    cannot silently report a finished triage as pending (BR-9, the T2.1 `is_classified` reasoning).
    """
    return SubmissionAcknowledgement(
        report_id=str(report.pk),
        status=str(report.status),
        issue_id=None,
        classification={"state": "ready" if report.is_classified else "pending"},
    )


def _require_citizen(author: User) -> None:
    """API §6.3 "Authorization: Citizen" — the single check both entry points call.

    Django's `PermissionDenied`, which the handler renders as the generic `403 FORBIDDEN`. API §6.3
    names no endpoint-specific code for this, and inventing one would put a contract decision in a
    service (the reasoning T1.9 recorded for `MeView`).

    ⚠️ **One function with two callers, not one check reimplemented twice.** `submit_report()` needs
    it *before* it touches the idempotency store, so that T2.1's observable ordering holds — a
    non-Citizen gets `403` and learns nothing else about the request, including whether their key
    was accepted. `create_report()` keeps calling it too: it is reachable on its own, and an
    authorization check that only runs on one path is not an authorization check.
    """
    if author.role != Role.CITIZEN:
        raise PermissionDenied("Only citizens may submit reports.")


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

    ⚠️ **`media_count` is passed in rather than counted here, and T2.6 did not change that.** At
    intake the media rows are not yet attached to the report — the client pre-uploads via §6.4 and
    sends `mediaIds` (§6.3) — so the count is only knowable *before* the report exists. T2.6 wires
    the real number: `submit_report()` resolves the ids through
    `media.services.resolve_media_for_attachment()`, which proves each one is real, owned by this
    citizen and not already attached, and hands back the count this rule reads.

    Keeping the parameter rather than querying `report.media` is what makes BR-3 checkable before
    the write. A caller supplying `0` gets the description rule, which is the honest answer for a
    text-only submission.
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
    _require_citizen(author)

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


def _human_classification_source(actor: User) -> str:
    """Which `ClassificationSource` a human correction records (FR-11).

    ⚠️ **An Admin records `AUTHORITY`, and the enum has no `ADMIN` member on purpose.** What this
    column answers is "who decided this category" at the granularity the pipeline cares about:
    the LLM, its fallback, the reporting citizen, or **a human official**. PRD §4.2 gives Admin every
    Authority capability, so an Admin re-categorizing is doing the Authority's job — and adding a
    fifth member would silently divide the "a human official set this" set in two, so every future
    query that has to exclude human decisions (T3.5's, below) would need updating and would fail open
    if it were not.
    """
    return (
        ClassificationSource.CITIZEN
        if actor.role == Role.CITIZEN
        else ClassificationSource.AUTHORITY
    )


def update_report(
    *,
    actor: User,
    report: Report,
    description: str | None = None,
    category_slug: str | None = None,
) -> Report:
    """`PATCH /reports/{id}` — the pre-triage edit and the human re-categorization (T2.8, FR-11).

    Two callers with two different rights, and §6.3 grants them separately:

    * **The author**, and *only while pre-triage*, may correct `description` and `category`. Past
      triage the answer is `409 NOT_EDITABLE`: the report has been classified and may already be
      clustered into an Issue an Authority is working, so rewriting the text underneath them would
      change what was triaged without re-triaging it.
    * **An Authority in scope, or an Admin**, may **re-categorize at any time** — that is the whole
      point of FR-11's correction path, which exists precisely because the LLM gets some wrong. They
      may not touch `description`: it is the citizen's own account of what they saw, and an official
      editing it is not a correction, it is a rewrite of evidence.

    ⚠️ **`None` means "not sent", which is why neither field accepts `null` at the HTTP layer.**
    `PATCH` is partial (api-conventions.md), so absence has to be expressible — and `description=""`
    is a real value a client may send (BR-3 lets a photo carry the report). `ReportPatchSerializer`
    refuses an explicit `null` for both fields, which is what makes this collapse unambiguous:
    clearing a category is not an edit, it is a request to re-run triage, and this endpoint does not
    do that.

    ⚠️ **No `severity` parameter, and its absence is the contract.** FR-11 also says authorities may
    "re-severity" — but severity lives on the **Issue**, never on the Report (data-model "Ownership &
    Permissions"; CLAUDE.md), so that override is §6.5's, not this one's. A `severity` key in the body
    is refused `400` by `reject_unknown_fields()` rather than dropped: dropping it would answer `200`
    to an Authority who believed they had just escalated a Critical report.

    ⚠️ **`classified_at` is deliberately NOT stamped by a human correction, and this is the trap.**
    Stamping it would look like an improvement — `is_classified` becomes true, so T3.5's worker skips
    the report and cannot revert the official's decision. What it actually produces is a report that
    *reads* as triaged while `severity_signal` is still `NULL`, and `SEVERITY_RANK[None]` raises
    inside BR-11's `max()` when T4.6 computes the Issue's severity. The report would be a landmine
    planted in a code path that does not exist yet.

    ⚠️ **So the protection T3.5 must implement is on `classification_source`, not on
    `classified_at`:** the triage worker must not overwrite the `category` of a report whose source is
    `ClassificationSource.AUTHORITY` (`_human_classification_source()` above is why an Admin lands
    there too). `CITIZEN` carries no such protection — §6.3 is explicit that a citizen's category is
    "a hint only" and FR-10 has the LLM decide.

    Raises:
        AuthorizationError: `403 FORBIDDEN` — an Authority outside the report's category scope.
        PermissionDenied: `403 FORBIDDEN` — a citizen who is not the author, or an official trying to
            edit the description.
        Conflict: `409 NOT_EDITABLE` — the author editing a report past triage, or any caller
            reaching a moderated row.
        ReportValidationError: `400 VALIDATION_FAILED` — an unknown or retired category slug, or an
            edit that would leave the report with neither a photo nor an adequate description (BR-3).

    ⚠️ **The moderation guard is unreachable from the endpoint and is kept anyway.**
    `ReportDetailView` routes through `get_report_for_read()`, which raises `410` for a hidden or
    removed report before this function is called. A management command holding a `Report` does not,
    and without the guard an Authority could re-categorize suppressed content back into a live
    queue — FR-31 undone by a code path nobody was looking at.

    ⚠️ **No `select_for_update`, and last-write-wins is the intended semantics here.** Two officials
    re-categorizing the same report concurrently are both making a correction; the second one's is
    the current answer. Locking the row would serialize a queue for no correctness gain, and the
    `updated_at` bump plus the FR-11 log line record both attempts.
    """
    if report.status in {ReportStatus.HIDDEN, ReportStatus.REMOVED}:
        raise Conflict("This report has been removed and can no longer be edited.")

    is_author = report.author_id == actor.pk
    official = has_role(actor, Role.AUTHORITY, Role.ADMIN)

    if official:
        # ⚠️ **`403`, not the usual `404`-to-see for an out-of-scope Authority.** T1.5's rule is that
        # a scoped *read* answers `404` so a `403` cannot confirm an id exists elsewhere — but
        # `GET /reports/{id}` is **public** (Q7), so this report's existence is not a secret this
        # endpoint could keep. A `404` here would be a lie the same client disproves with one `GET`,
        # and §6.3's error list for `PATCH` names `FORBIDDEN`.
        #
        # ⚠️ An **unclassified** report has no category, so no Authority is in scope for it and this
        # raises for all of them. That is consistent with `list_reports()` — a report nobody has
        # categorized belongs to no department — and Admins bypass it, as `has_category_scope()`
        # documents. The un-triaged backlog is T3.5's to clear, not an Authority's to claim.
        if report.category is None:
            raise PermissionDenied("This report has not been categorized yet.")
        require_category_scope(actor, report.category)
        if description is not None:
            raise PermissionDenied("Only the author may edit a report's description.")
    elif not is_author:
        # ⚠️ Deliberately the same generic `403` an official gets, and it names neither the role nor
        # the reason (T1.5: "denial messages name neither role nor resource").
        raise PermissionDenied("You do not have permission to edit this report.")
    elif not report.is_editable:
        # ⚠️ §6.3's own code, not the generic `CONFLICT`. "Already triaged" is a state a client can
        # act on — stop offering the edit affordance — while `CONFLICT` is indistinguishable from a
        # duplicate submission.
        raise Conflict(
            "This report has already been triaged and can no longer be edited.",
            code="NOT_EDITABLE",
        )

    changed: list[str] = []
    # ⚠️ Read through the object rather than the `category_id` column, and read it **before** the
    # mutation below — FR-11's log wants the *pair* (what was there, what the human chose), and after
    # the assignment the old slug is gone. Going through `category` costs nothing extra: Django
    # answers `None` for a `NULL` FK without touching the database, and it is the only spelling a
    # type-checker can narrow (the id column tells it nothing about the relation being non-`None`).
    previous_category = report.category
    previous_slug = previous_category.slug if previous_category else None

    if description is None and category_slug is None:
        # ⚠️ **Checked *after* authorization, not before.** A caller who may not edit this report must
        # not be able to distinguish "your body was empty" from "you may not touch this" — the same
        # observable ordering `submit_report()` records for the idempotency key. `ReportPatchSerializer`
        # refuses an empty body first, so this is the guard for a non-HTTP caller (FR-3): without it
        # the function answers `200` and a bumped `updated_at` to a request that changed nothing.
        raise ReportValidationError(
            {"description": "Send a description or a category to change."},
            code="REQUIRED",
        )

    if description is not None:
        # ⚠️ **BR-3 is re-checked on the edit, against the photos the report still has.** The rule is
        # "a photo *or* an adequate description"; an author blanking the description of a report whose
        # only photo was removed under FR-31 would otherwise leave a submission with no content at
        # all — a state `POST /reports` cannot produce, reached by editing.
        validate_report_content(
            description=description,
            media_count=media_selectors.visible_media_count(report_id=report.pk),
        )
        report.description = description.strip()
        changed.append("description")

    if category_slug is not None:
        # Reuses intake's resolver, so a retired slug is refused here too — the same reasoning:
        # BR-7's coercion to `Other` is for an *LLM* returning something off-taxonomy, and silently
        # filing an official's correction under `Other` loses the decision they just made.
        report.category = _resolve_category(category_slug)
        report.classification_source = _human_classification_source(actor)
        changed.extend(["category", "classification_source"])

    # ⚠️ `updated_at` must be listed: it is `auto_now`, and `save(update_fields=...)` writes only the
    # named columns, so omitting it leaves the row reading as never-modified — the same trap
    # `submit_report()`'s status flip records.
    report.save(update_fields=[*changed, "updated_at"])

    current_category = report.category
    logger.info(
        # ⚠️ **FR-11's "corrections are logged", and this line is the whole of it today.** The
        # requirement continues "and can seed prompt examples / evaluation sets", which needs the
        # *pair* (what the machine said, what the human chose) — so both slugs are here, not just the
        # new one. A structured log rather than a table for the same reason BR-25's audit is: the
        # immutable store is T8.1, and this is the call site it replaces.
        "report.updated",
        report_id=str(report.pk),
        # ⚠️ No description text, before or after. NFR-12 keeps PII out of logs, and the description
        # is the citizen's own account of where they were standing. That it *changed* is the
        # auditable fact; what it says is on the row.
        description_changed="description" in changed,
        category_from=previous_slug,
        category_to=current_category.slug if current_category else None,
        actor_role=str(actor.role),
        by_author=is_author,
    )
    return report


def _submission_fingerprint(
    *,
    author: User,
    location: Point | None,
    description: str,
    category_slug: str | None,
    address: str,
    language: str,
    media_ids: Sequence[UUID | str],
) -> str:
    """The normalized view of a submission that two requests must share to be "the same" (§4.6).

    ⚠️ **Normalized, not the raw body** — §4.6 fixes this. `description` and `address` are stripped
    exactly as `create_report()` strips them before writing, and the coordinate is reduced to the
    two floats that will be stored. A client that resends with different key ordering, trailing
    whitespace, or `90.3990` for `90.399` is making the same submission, and answering `409` to that
    would turn the retry safety net into the fault it exists to prevent.

    ⚠️ **`author` is in the fingerprint even though the cache key is already per-user.** Belt and
    braces is not the reason: the key is a *hash*, so a collision — or a future scope that widens —
    would otherwise let one citizen's stored response be handed to another. The cost is one field.

    ⚠️ **`location is None` fingerprints as `None` rather than raising.** The key is resolved before
    validation (so that a rejected request does not consume it), which means this function must
    tolerate every input the caller could pass, including the invalid ones.

    ⚠️ **T2.6: the media *ids* are fingerprinted, sorted — not the count.** Two submissions that
    carry the same text at the same spot but different photos are different submissions, and a count
    cannot tell them apart: a retry that swapped one photo for another would replay the first
    response and the second photo would be silently discarded. Sorted because `mediaIds` is a set in
    meaning and a list in JSON, so a reordering client is making the same submission (§4.6
    "normalized"). Ids are opaque UUIDs, so unlike a description they carry nothing about the
    citizen.
    """
    payload: dict[str, Any] = {
        "author": str(author.pk),
        "description": description.strip(),
        "category": category_slug,
        "address": address.strip(),
        "language": language,
        "media_ids": sorted(str(value) for value in media_ids),
        "lng": None if location is None else location.x,
        "lat": None if location is None else location.y,
    }
    return idempotency.fingerprint(payload)


@transaction.atomic
def submit_report(
    *,
    author: User,
    location: Point | None,
    description: str = "",
    category_slug: str | None = None,
    address: str = "",
    language: str = "en",
    media_ids: Sequence[UUID | str] | None = None,
    idempotency_key: str | None = None,
) -> SubmissionAcknowledgement:
    """Intake for `POST /reports` (T2.2, T2.3): persist, mark processing, enqueue triage (FR-5, NFR-3).

    The synchronous half of Arch §4's flow. Everything expensive — the LLM call, clustering,
    notification — happens in the worker, so the citizen's request returns as soon as the row is
    durable. API §6.3 answers `202`, not `201`, precisely because triage has not run yet.

    ⚠️ **The enqueue is registered with `transaction.on_commit`, and that is not a style
    preference.** Calling `.delay()` inline races the transaction: Redis is not transactional, so
    an idle worker can pick the message up and `SELECT` the report *before* this transaction
    commits — it reads nothing, and the report is never classified. The window is small, real, and
    load-dependent, so it survives every local test and appears under production concurrency
    (async-worker.md: "This is not optional"; Arch §4.1).

    ⚠️ **A rolled-back transaction therefore enqueues nothing**, which is the other half of the
    same guarantee: no task ever references a report that does not exist.

    ⚠️ **Only the id crosses the broker boundary**, bound to a local before the closure is built.
    Capturing `report` would invite passing the instance itself, which puts a whole row (author id,
    free text, coordinates) into Redis and lets the worker act on a pre-commit snapshot instead of
    re-reading the committed row.

    ⚠️ **`status` moves to `PROCESSING` here, in a second UPDATE inside the same transaction, and
    not by passing a status into `create_report()`.** T2.1 refuses that parameter on purpose — it
    is the thing that would let a caller mark a report `triaged` and skip the pipeline. The two
    writes share one transaction, so no reader ever observes a submitted-but-never-queued row.

    ---

    ⚠️ **T2.3 — the key is resolved BEFORE the write, and the ordering of the four steps is the
    whole design** (plan T2.3: "resolved in the service before the write"):

    1. **Authorization first**, so T2.1's observable ordering survives — a non-Citizen gets `403`
       and learns nothing else, not even whether their key was free.
    2. **Reserve second**, before validation. A replay then short-circuits without re-validating,
       which is both cheaper and correct: the original already passed. Reserving *after* validation
       would still be race-safe (`cache.add()` is atomic either way) but would run the duplicate's
       validation for nothing.
    3. **Release on any failure**, so §4.6's "a request that fails validation does not consume its
       key" holds and the client can fix the body and retry with the same key.
    4. **Complete from `on_commit`**, never inline — see `idempotency.complete()`. Until the
       transaction commits the key stays *in progress*, so a concurrent double-tap gets
       `409 IDEMPOTENCY_IN_PROGRESS` rather than a replay of a report that does not exist yet.
       That in-flight window is the guarantee, not an artifact.

    ⚠️ **Without a key there is no de-duplication, by design** (§4.6: the header is optional and
    carries "no de-duplication guarantee" when absent). Inferring a key from the payload would
    silently refuse a citizen who really is filing two reports about the same pothole from the same
    spot — which FR-16 counts as two corroborating voices, not a mistake.

    ---

    ⚠️ **T2.6 — `mediaIds` is resolved BEFORE the write and attached AFTER it, and it has to be
    both.** BR-3 accepts a photo-only submission, so the report cannot be created until the photos
    are known to be real, owned by this citizen and not already spent — but there is nothing to
    attach them to until the row exists. `resolve_media_for_attachment()` proves and counts;
    `attach_media()` binds. Both run inside this transaction, so a failure at either end leaves
    neither a report with phantom evidence nor photos bound to a report that rolled back.

    ⚠️ **The resolve runs before `create_report()`, which means before the location check.** A
    client sending five unusable ids *and* an out-of-city point is told about the ids first. That is
    a deliberate departure from T2.1's "authorization → location → content" ordering for one reason:
    `media_count` is an *input* to the content rule (BR-3), so it cannot be established after it.
    """
    _require_citizen(author)

    ids = list(media_ids or [])
    key = idempotency.normalize_key(idempotency_key)
    reservation: idempotency.Reservation | None = None
    if key is not None:
        outcome = idempotency.reserve(
            scope=IDEMPOTENCY_SCOPE,
            user_id=author.pk,
            key=key,
            request_fingerprint=_submission_fingerprint(
                author=author,
                location=location,
                description=description,
                category_slug=category_slug,
                address=address,
                language=language,
                media_ids=ids,
            ),
        )
        if isinstance(outcome, idempotency.Replay):
            # ⚠️ The stored payload is rehydrated rather than re-serialized, and `replayed` is
            # forced on here — the flag is never persisted as `True`, so a record cannot come back
            # claiming to be a replay of a replay.
            acknowledgement = SubmissionAcknowledgement(**{**outcome.payload, "replayed": True})
            logger.info(
                "report.submission.replayed",
                report_id=acknowledgement.report_id,
                # ⚠️ The key itself is never logged: it is client-supplied and may carry anything,
                # and a log line is exactly where a key becomes reusable by someone else (NFR-12).
                scope=IDEMPOTENCY_SCOPE,
            )
            return acknowledgement
        reservation = outcome

    try:
        # ⚠️ Inside the `try`, so a bad id releases the idempotency key like any other failure — the
        # client's next move is a corrected `mediaIds`, which is a different fingerprint anyway.
        media_count = media_services.resolve_media_for_attachment(owner=author, media_ids=ids)

        report = create_report(
            author=author,
            location=location,
            description=description,
            category_slug=category_slug,
            address=address,
            language=language,
            media_count=media_count,
        )

        # ⚠️ After the row exists and before the `PROCESSING` flip: `attach_media()` deliberately
        # carries no `@transaction.atomic` of its own, so it joins this one. If the status write
        # below fails, the photos come unattached again with it.
        media_services.attach_media(report=report, owner=author, media_ids=ids)

        report.status = ReportStatus.PROCESSING
        # ⚠️ `updated_at` must be listed: it is `auto_now`, and `save(update_fields=...)` writes only
        # the named columns, so omitting it leaves the row's timestamp reading as never-modified.
        report.save(update_fields=["status", "updated_at"])

        report_id = str(report.pk)
        transaction.on_commit(lambda: classify_report.delay(report_id))
        acknowledgement = _acknowledge(report)
    except Exception:
        # ⚠️ Covers every failure inside the write, not just validation — an out-of-city `422`, a
        # database error, anything. The alternative is a key held for the full retention window by a
        # request that produced nothing. A caller-wrapped transaction that rolls back *after* this
        # function returns is the one case this cannot see; the short in-progress TTL is the
        # backstop there, which is why it exists.
        if reservation is not None:
            idempotency.release(reservation)
        raise

    if reservation is not None:
        held = reservation
        payload = asdict(acknowledgement)
        transaction.on_commit(lambda: idempotency.complete(held, payload=payload))

    logger.info(
        "report.submitted",
        report_id=acknowledgement.report_id,
        # ⚠️ No description, no address, no coordinate. NFR-12 keeps PII out of logs, and a
        # report's free text is the citizen's own account of where they are.
        category_hint=category_slug,
        language=language,
        media_count=media_count,
        idempotent=key is not None,
    )
    return acknowledgement
