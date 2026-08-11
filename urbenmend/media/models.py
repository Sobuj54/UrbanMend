"""
Media — persistence (T2.4).

Upload handling, server-side compression, thumbnailing, EXIF stripping, storage keys.

⚠️ **A Media row is created *unattached*, and that is the contract, not an oversight.** API §6.4
uploads a photo and hands back a handle; §6.3 then submits a report carrying `mediaIds`. So between
those two calls a Media exists with no Report, which is why `report` is nullable and why `owner` is
a column of its own — during that window `owner` is the only thing authorization can key on.

⚠️ **Nothing EXIF-bearing is ever written to storage** (P3, BR-4, ❓Q6 RESOLVED 2026-08-08: strip
all EXIF always). The sanitizing re-encode happens in `services.upload_media()`, in the request,
before the first save — see the reasoning there for why that step is not deferred to the worker
even though the *derivative* work is.

[doc: Arch §3 (FR-7, P3); data-model §4; API §6.4; BR-4]
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class MediaState(models.TextChoices):
    """The processing lifecycle of one photo (data-model §4 "Lifecycle").

    `Uploaded → Processing → Ready`, with `→ Failed (retry)` and `→ Removed (moderated)` branches.

    ⚠️ **`READY` describes the *derivatives*, not the safety of the original.** The stored master is
    already EXIF-free in `UPLOADED` — stripping is synchronous (see the module docstring). What
    `PROCESSING` is waiting on is the downscale and the thumbnail. Reading `READY` as "now it is
    safe to serve" would invert that and imply the earlier states are not.

    ⚠️ **`FAILED` is not a delete.** data-model §4 marks the branch "retry", and a row that vanished
    on a transient storage error would take its Report's BR-3 justification with it — a report that
    was valid at submission would retroactively have no photo and no adequate description.

    ⚠️ **`REMOVED` is how moderation deletes** (FR-31, database.md "No hard deletes"). The read
    answers `410 GONE` (API §4.2), which is only expressible while the row still exists.
    """

    UPLOADED = "uploaded", _("Uploaded (sanitized, awaiting derivatives)")
    PROCESSING = "processing", _("Processing (compression and thumbnail)")
    READY = "ready", _("Ready")
    FAILED = "failed", _("Processing failed")
    REMOVED = "removed", _("Removed by moderation")


def _upload_path(instance: Media, filename: str) -> str:
    """Storage key for the sanitized master.

    ⚠️ **Keyed on the Media id, never on the client's filename.** An uploaded name is attacker-
    controlled and routinely carries PII (`IMG_at_home.jpg`), and object keys surface in presigned
    URLs, access logs and bucket listings (NFR-12). `filename` reaches here as the extension the
    re-encode chose, so the key always matches the bytes.
    """
    return f"reports/{instance.pk}/original{filename}"


def _thumbnail_path(instance: Media, filename: str) -> str:
    """Storage key for the derived thumbnail — same reasoning as `_upload_path`."""
    return f"reports/{instance.pk}/thumb{filename}"


class Media(models.Model):
    """One photo attached (or attachable) to a Report as visual evidence (FR-7, data-model §4).

    ⚠️ **Validation lives in `services.py`** (FR-3, Arch §2.4). Size, format and decodability are
    all properties of the *bytes*, which the model never sees; `upload_media()` owns them, so a
    management command or a future bulk importer gets the same three rejections the endpoint does.
    """

    # Opaque, non-sequential (API §1.2 — no IDOR). Same reasoning as `Report.id`, and here it is
    # also the storage key prefix, so a guessable id would make bucket objects enumerable.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ⚠️ **Nullable, and null is the normal state at upload** — see the module docstring. A
    # non-null column would force §6.4 to accept a `reportId` it does not have, which would mean
    # uploading only after submitting, and BR-3 needs the count *at* submission.
    #
    # ⚠️ **`PROTECT`, not `CASCADE`.** Reports are never hard-deleted (database.md), so this branch
    # should be unreachable — and that is the point of making it fail loudly. `CASCADE` would
    # quietly drop the rows while leaving every object behind in the bucket, so the only record of
    # what is still stored would be gone. Same posture as `Report.author`.
    report = models.ForeignKey(
        "reporting.Report",
        on_delete=models.PROTECT,
        related_name="media",
        null=True,
        blank=True,
    )

    # ⚠️ **The uploader, recorded separately from `report.author` and not derived from it.**
    # Authorization on `POST /media` and on the T2.6 attach both run while `report` is still null,
    # so this is the only owner there is at that point. It also keeps the attach step honest: a
    # citizen may only attach photos they uploaded, which is a comparison this column makes possible
    # and `report.author` cannot.
    #
    # `PROTECT` for C-14 — deletion anonymizes the User row rather than removing it, so no cascade
    # should ever fire here either.
    owner = models.ForeignKey(
        "identity.User",
        on_delete=models.PROTECT,
        related_name="media",
    )

    state = models.CharField(
        max_length=16,
        choices=MediaState.choices,
        default=MediaState.UPLOADED,
        db_index=True,
    )

    # The sanitized master: EXIF-stripped at upload, downscaled by the worker (T2.5).
    file = models.FileField(upload_to=_upload_path, max_length=255)
    # ⚠️ Blank until the worker produces it, which is why §6.4's `202` body sends
    # `"thumbnailUrl": null` — the field is empty, not the URL builder failing.
    thumbnail = models.FileField(upload_to=_thumbnail_path, max_length=255, blank=True)

    # The detected format, recorded rather than trusted from the request: `Content-Type` is
    # client-supplied and `upload_media()` overrides it with what Pillow actually decoded.
    image_format = models.CharField(max_length=16)
    byte_size = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()

    # ⚠️ Why processing failed, for an operator — not for the client. §6.4 has no error field on
    # the media resource, and a decoder's message can quote file contents.
    failure_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "media_media"
        verbose_name = _("media")
        verbose_name_plural = _("media")
        ordering = ["created_at"]
        indexes: ClassVar[list[models.Index]] = [
            # `GET /reports/{id}` renders `media[]` per report (§6.3), and T2.6 counts a report's
            # photos on the attach path.
            models.Index(fields=["report", "created_at"], name="media_media_report_idx"),
            # The T2.6 attach lookup: this citizen's unattached uploads. Partial on `report IS
            # NULL` because that is the only rowset the query wants, and unattached media are a
            # small, transient minority of the table.
            models.Index(
                fields=["owner", "created_at"],
                name="media_media_unattached_idx",
                condition=models.Q(report__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"Media {self.pk}"

    @property
    def is_visible(self) -> bool:
        """Whether a public read may render this photo (Q7 RESOLVED: public; FR-31).

        ⚠️ Moderation is the only thing this hides. A `FAILED` or still-`PROCESSING` photo is
        visible — §6.4's `GET` returns the `state` precisely so a client can render a placeholder
        rather than being told the resource does not exist.
        """
        return self.state != MediaState.REMOVED
