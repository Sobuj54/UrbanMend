"""
Reporting — persistence (T2.1).

Report intake, validation, media association, submission idempotency, "me-too"/confirm,
comments. T2.1 builds the `Report` entity itself; media association lands with T2.4/T2.6,
idempotency with T2.3, confirmations with T4.7.

⚠️ **A Report is one submission; an Issue is a cluster of Reports** (PRD §6.1, CLAUDE.md). They
are not synonyms. **Severity, status, and assignment live on the Issue** — the only status here
is the Report's own *processing* lifecycle, and the Report never carries resolution status
(data-model §2 "Lifecycle").

[doc: Arch §3 (FR-5, FR-8, FR-9, FR-11); data-model §2; BR-1..BR-6, BR-35; C-3, C-5, C-11]
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GistIndex
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ReportStatus(models.TextChoices):
    """The Report's **processing** lifecycle (data-model §2 "Lifecycle").

    ⚠️ **Not the Issue workflow, and deliberately not a superset of it.** `Acknowledged`,
    `In Progress`, `Resolved` and `Closed` are Issue statuses (PRD §6.3) and must never appear
    here: "The Report never holds resolution status. Once `Triaged`, its progress is expressed
    through its Issue" (data-model §2). Adding a resolution value here would give two rows one
    answer to "is this fixed?" and let them disagree.

    `Draft` is omitted: FR-8 marks it SHOULD and offline draft sync is a PWA client concern
    (PRD §5.2). Adding the value now would ship a state nothing can reach or leave.
    """

    SUBMITTED = "submitted", _("Submitted")
    PROCESSING = "processing", _("Processing (awaiting classification)")
    TRIAGED = "triaged", _("Triaged (attached to an Issue)")
    # Moderation branch (FR-31). The moderated read returns `410` (API §4.2) rather than `404`,
    # which is why removal is a status and not a row delete (database.md "No hard deletes").
    HIDDEN = "hidden", _("Hidden by moderation")
    REMOVED = "removed", _("Removed by moderation")


class SeveritySignal(models.TextChoices):
    """The four severity bands (FR-14, C-1, Q2 RESOLVED).

    ⚠️ **Four bands, not three.** `03-data-model.md` §3 and BR-8 still show a stale
    `{High, Medium, Low}` set; the 4-band enum is authoritative (database.md, CLAUDE.md).

    ⚠️ **Declared here, in `reporting`, and shared with `issues`.** The Report carries severity
    as a *classification signal* (DM-A6: the classification result is an attribute of the Report,
    not a separate entity); the Issue carries the *authoritative, possibly-overridden* level
    (BR-11 = max of members). Two independently declared enums would let one gain a band the
    other lacks, and `max()` across them would then be undefined.

    ⚠️ **No numeric weight, and never a score** — FR-21 removed the one tunable numeric score,
    so ordering is by label (PRD §5.4). `SEVERITY_RANK` below exists only so BR-11's "highest"
    is computable; it is not a score, not tunable, and not exposed by the API.
    """

    CRITICAL = "critical", _("Critical")
    HIGH = "high", _("High")
    MEDIUM = "medium", _("Medium")
    LOW = "low", _("Low")


# ⚠️ Ordering for BR-11's "highest severity among member reports" (T4.6), nothing more. It is
# **not** a priority score (FR-21 removed those) and must not be summed, averaged, weighted, or
# combined with corroboration or proximity, both of which are display-only (C-10).
SEVERITY_RANK: dict[str, int] = {
    SeveritySignal.CRITICAL: 4,
    SeveritySignal.HIGH: 3,
    SeveritySignal.MEDIUM: 2,
    SeveritySignal.LOW: 1,
}


class ClassificationSource(models.TextChoices):
    """Which path produced the classification (FR-10, FR-13a, Arch §6).

    Recorded because FR-15 requires severity to be *explainable* and NFR-4 requires the LLM to be
    non-load-bearing: an operator has to be able to tell "the LLM said High" from "the LLM was
    down and a keyword rule said High". `CITIZEN`/`AUTHORITY` cover FR-11's human corrections.
    """

    LLM = "llm", _("Hosted LLM")
    FALLBACK = "fallback", _("Keyword fallback")
    CITIZEN = "citizen", _("Citizen-supplied at submission")
    AUTHORITY = "authority", _("Authority re-categorization")


class Report(models.Model):
    """A single citizen submission describing one observed problem at one place and time (FR-5).

    ⚠️ **Validation is enforced in `services.py`, not only here** (FR-3, Arch §2.4). Two of the
    three T2.1 rules cannot be model-level: BR-3 ("at least one of {photo, adequate description}")
    depends on `media`, which does not exist until T2.4, and BR-35 (inside the city boundary) is a
    PostGIS point-in-polygon query against reference data. `create_report()` owns both; a
    `full_clean()` here would additionally be skipped entirely by DRF, which never calls it.
    """

    # Opaque, non-sequential (API §1.2 — no IDOR). Same reasoning as `User.id`.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # BR-1 — exactly one author, and Q4/DM-Q2 RESOLVED means never null: anonymous reporting is
    # not supported, so a nullable author would model a state the product does not have.
    #
    # ⚠️ **`PROTECT`, not `CASCADE`.** C-14 requires that deleting a user neither orphans nor
    # destroys public Issue history, and BR-33/T1.9 implements deletion as *anonymization* — the
    # row survives with its PII nulled, so no cascade should ever fire. `PROTECT` makes a
    # hard-delete attempt fail loudly instead of silently erasing the reports that give an Issue
    # its corroboration count (FR-16).
    author = models.ForeignKey(
        "identity.User",
        on_delete=models.PROTECT,
        related_name="reports",
    )

    # FR-5 — free text. Blank is permitted at the *column* level because BR-3 accepts
    # photo-only submissions; the "at least one of" rule is checked in the service, where the
    # media count is known.
    description = models.TextField(blank=True)

    # ⚠️ **Nullable, and null is the normal state at creation.** Category arrives from async
    # classification (FR-10, BR-9: "a Report validly exists before it is classified"), so a
    # non-null column would force the intake path to guess — exactly what C-2's controlled
    # taxonomy exists to prevent. A client-supplied value is a *hint* (API §6.3) recorded with
    # `classification_source = citizen`.
    #
    # `PROTECT` because categories retire rather than delete (data-model §5) — a delete that
    # would strand classified reports should fail.
    category = models.ForeignKey(
        "classification.Category",
        on_delete=models.PROTECT,
        related_name="reports",
        null=True,
        blank=True,
    )

    # ⚠️ **The authoritative location (BR-2, BR-4, C-3), and it is the explicit report
    # coordinate — never photo EXIF**, which is stripped by default (P3, T2.5). Non-nullable:
    # BR-2 says a Report cannot be submitted without one, so nullability would make the
    # constraint a convention.
    #
    # `geography=True`, SRID 4326 — matches `CityBoundary.area` so the BR-35 point-in-polygon
    # compares like with like, and gives metre-based distances for T4.4 clustering (Arch §9).
    location = gis_models.PointField(
        geography=True,
        srid=4326,
        spatial_index=False,  # named explicitly in Meta.indexes below
    )

    # FR-6 — "store precise coordinates **and** reverse-geocoded address". Blank-able because
    # reverse geocoding is an external dependency (ASSUMP-5) and Arch §409 requires it to
    # degrade to coordinates-only rather than block a submission.
    address = models.CharField(max_length=255, blank=True)

    # FR-12 — the submission's language, so bilingual classification and notifications can be
    # correct. Reuses `identity.Language` rather than redeclaring `en`/`bn`.
    language = models.CharField(
        max_length=8,
        choices=[("en", _("English")), ("bn", _("Bangla"))],
        default="en",
    )

    status = models.CharField(
        max_length=16,
        choices=ReportStatus.choices,
        default=ReportStatus.SUBMITTED,
        db_index=True,
    )

    # --- Classification result (DM-A6: attributes of the Report, not a separate entity) -----
    # All nullable: BR-9 makes classification asynchronous, so every one of these is empty
    # between `POST /reports` returning `202` and the worker finishing (T3.5).
    #
    # ⚠️ **`noqa: DJ001` on the two choice columns — nullable is deliberate, against ruff's
    # default advice.** DJ001 warns that a nullable string column has two spellings for
    # "empty" (`NULL` and `""`); that is the right default and is why `classification_model`
    # and `classification_rationale` below are `blank=True` with no `null`. It is wrong here
    # for two reasons:
    #   1. `""` is not a member of `SeveritySignal`, so it would be an undeclared fifth band
    #      that only validates because Django skips blank values — and `SEVERITY_RANK[""]`
    #      raises `KeyError` inside T4.6's BR-11 `max()`, at runtime.
    #   2. `confidence` is a `FloatField` and must be `NULL` when unclassified. Spelling
    #      "not yet classified" as `NULL` in one column of this block and `""` in another
    #      means the worker's queue query (T3.5) needs to know which convention applies per
    #      column, and a wrong guess silently matches nothing.
    severity_signal = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=SeveritySignal.choices,
        null=True,
        blank=True,
    )
    # FR-10 stores the confidence; ❓Q10 (the accuracy bar / low-confidence threshold) is
    # **open**, so no threshold is encoded here — only the value. T3.7 owns the flagging.
    confidence = models.FloatField(null=True, blank=True)
    classification_source = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=ClassificationSource.choices,
        null=True,
        blank=True,
    )
    # FR-10 — "the model/provider + version used", and FR-15 needs the rationale to be
    # retained so severity is explainable. ❓Q9 (LLM provider) is open, so this is a free-text
    # record of whatever the adapter reports, not an enum of known providers.
    classification_model = models.CharField(max_length=100, blank=True)
    classification_rationale = models.TextField(blank=True)
    classified_at = models.DateTimeField(null=True, blank=True)
    # T3.7 - an internal queueing signal for later human review. Q10 has not fixed the
    # confidence threshold, so the worker leaves this false unless a deployment explicitly
    # configures one. It is intentionally not exposed by the v1 Report serializer yet.
    classification_needs_review = models.BooleanField(default=False, db_index=True)

    # BR-6 / C-5 — at most one Issue at a time, null until clustering attaches it (T4.5).
    # `PROTECT` keeps citizen evidence and authority work history intact: Issues are moderated or
    # merged, never hard-deleted (database.md "No hard deletes").
    issue = models.ForeignKey(
        "issues.Issue",
        on_delete=models.PROTECT,
        related_name="reports",
        null=True,
        blank=True,
    )

    # Server-authoritative timestamp (FR-5) — never client-supplied, or a client could
    # backdate a report to win an age-based sort (FR-19).
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reporting_report"
        verbose_name = _("report")
        verbose_name_plural = _("reports")
        # API §6.3 — `GET /reports` sorts `-createdAt` by default.
        ordering = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            # ⚠️ **`GistIndex`, not `models.Index`** — the latter emits a B-tree, which cannot
            # serve `ST_DWithin` or a bbox and would leave every spatial query a sequential scan
            # while reading as indexed in the migration. `spatial_index=False` on the field
            # prevents a duplicate auto-named index (database.md, NFR-1). Every spatial consumer
            # depends on this: BR-35 containment, T4.4 clustering, the T7.4 map bbox.
            GistIndex(fields=["location"], name="reporting_report_location_gist"),
            # `GET /reports` for a citizen tracking their own submissions (FR-1, API §6.3),
            # in the default `-createdAt` order.
            models.Index(
                fields=["author", "-created_at"],
                name="reporting_report_author_idx",
            ),
            # The classification worker's queue read: unclassified reports oldest-first (T3.5).
            models.Index(
                fields=["status", "created_at"],
                name="reporting_report_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Report {self.pk}"

    @property
    def is_classified(self) -> bool:
        """Whether async triage has produced a result (BR-9).

        Keyed on `classified_at`, not on `category`: a citizen-supplied category hint populates
        `category` at intake (API §6.3), so `category is not None` would report an unclassified
        report as classified and let the worker skip it.
        """
        return self.classified_at is not None

    @property
    def is_editable(self) -> bool:
        """Whether the author may still edit (FR-11, API §6.3 `409 NOT_EDITABLE`).

        Pre-triage only. T2.8 owns the endpoint; the rule lives here because both the service
        and the serializer need one answer to the question.
        """
        return self.status in {ReportStatus.SUBMITTED, ReportStatus.PROCESSING}
