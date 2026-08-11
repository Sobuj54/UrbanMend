"""
Media — write operations (T2.4/T2.6).

Every state change and every authorization check for photos lives here (FR-3, Arch §3.1). Views
stay thin; `reporting/services.py` calls in here rather than touching `Media` rows itself.

Rules for this file [doc: Arch §3.1, FR-3]:
  - Callers pass the acting user; functions authorize before mutating. DRF permission
    classes are defence-in-depth, never the enforcement point.
  - Wrap multi-write operations in `transaction.atomic`.
  - Enqueue Celery tasks via `transaction.on_commit` so a worker cannot observe an
    uncommitted row [doc: Arch §2.4, §4.1].
  - Reads belong in selectors.py.

⚠️ **The EXIF strip is synchronous; only the derivatives are deferred.** §6.4 describes the pipeline
as asynchronous and answers `202`, while `docs/08-coding-workflow.md` §C3 says "Never store the
original EXIF-bearing file". Both hold at once only if the sanitizing re-encode happens *before* the
first save — which is what `upload_media()` does. Writing the raw upload and stripping in the worker
would put a GPS-tagged photo of a citizen's home in object storage for as long as the queue is deep,
and every retry, dead-letter or worker outage extends that window (P3, BR-4, NFR-12).

The step is cheap and already paid for: deciding between `415` and `422` requires decoding the
image, so by the time the policy check finishes the pixel buffer is already in memory. What stays in
the worker is the genuinely expensive part — LANCZOS downscaling and thumbnail generation (T2.5).

[doc: API §6.4; FR-7, FR-31, P3, BR-3, BR-4; data-model §4]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import structlog
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from urbenmend.api.exceptions import (
    Conflict,
    PayloadTooLarge,
    UnprocessableEntity,
    UnsupportedMediaType,
)
from urbenmend.identity.models import Role, User
from urbenmend.media import imaging
from urbenmend.media.models import Media, MediaState
from urbenmend.media.tasks import process_media

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from django.core.files.uploadedfile import UploadedFile

    from urbenmend.reporting.models import Report

logger = structlog.get_logger(__name__)


class MediaValidationError(ValidationError):
    """A rejection the client can act on by sending different ids.

    A Django `ValidationError` subclass so `urbenmend_exception_handler` renders the `400` envelope
    without this module importing DRF — the posture `ReportValidationError` takes.
    """


def _require_citizen(actor: User) -> None:
    """API §6.4 "Authorization: Citizen" — one check, every entry point.

    Django's `PermissionDenied` → the generic `403 FORBIDDEN`. §6.4 names no endpoint-specific code,
    and inventing one would put a contract decision in a service (the T1.9/T2.1 reasoning).
    """
    if actor.role != Role.CITIZEN:
        raise PermissionDenied("Only citizens may upload photos.")


def _reject_undecodable(
    exc: imaging.UnreadableImage, *, owner: User, declared: str | None
) -> NoReturn:
    """Log for operators, raise the client-safe `422` (API §6.4 "corrupt image").

    ⚠️ **The decoder's own message never reaches the client.** Pillow quotes header bytes in some of
    its errors, so a crafted upload could otherwise echo chosen content back out through the error
    envelope. Operators get the real text in the log line, correlated by `traceId`.

    ⚠️ **`422`, not `400`.** The request is well-formed multipart carrying a real file; what fails
    is a rule about its contents. §6.4 lists `422` for exactly this, next to `413` and `415`.
    """
    logger.warning(
        "media.undecodable",
        owner_id=str(owner.pk),
        declared_content_type=declared,
        detail=str(exc),
    )
    raise UnprocessableEntity("This image could not be read. Try uploading the photo again.")


def upload_media(*, owner: User, upload: UploadedFile) -> Media:
    """Accept one photo: validate, sanitize, store, enqueue derivatives (FR-7, API §6.4).

    ⚠️ **The three rejections are checked in cost order, and the order is observable.** Size first
    (a length comparison, no decode), then format, then decodability. Reversed, a 500 MB file would
    be fully decoded before being refused for its size — the shape of an upload-based DoS, and
    `POST /media` ships unthrottled until T2.9 in exactly the way `POST /reports` does.

    ⚠️ **`upload.size` is trusted for the bound, and that is safe here.** It is Django's own count
    of bytes received, not a client-declared `Content-Length`: a `MemoryUploadedFile` knows its
    buffer length and a `TemporaryUploadedFile` knows its file size. A client that lies about
    `Content-Length` is caught by the transport, not by this line.

    ⚠️ **State moves `UPLOADED → PROCESSING` in a second save inside the same transaction**, and the
    enqueue rides `transaction.on_commit` — the T2.2 pattern, for the same reason: Redis is not
    transactional, so an inline `.delay()` lets an idle worker `SELECT` this row before it commits,
    find nothing, and leave the photo without a thumbnail forever. Load-dependent, so it survives
    every local run.
    """
    _require_citizen(owner)

    max_bytes = settings.MEDIA_MAX_UPLOAD_BYTES
    if upload.size is None or upload.size > max_bytes:
        raise PayloadTooLarge(
            f"This photo exceeds the {max_bytes // (1024 * 1024)} MB upload limit."
        )
    if not upload.size:
        # An empty part is a broken client, not a corrupt image: nothing was uploaded at all, so
        # "take the photo again" would be the wrong instruction.
        raise MediaValidationError(
            {"file": "The uploaded file is empty."}, code="VALIDATION_FAILED"
        )

    raw = upload.read()

    # ⚠️ Detected, never taken from `upload.content_type` — that header is client-supplied, and a
    # renamed executable arrives with a perfectly respectable `image/jpeg` on it.
    try:
        detected = imaging.detect_format(raw)
    except imaging.UnreadableImage as exc:
        _reject_undecodable(exc, owner=owner, declared=upload.content_type)

    if detected not in settings.MEDIA_ALLOWED_IMAGE_FORMATS:
        raise UnsupportedMediaType(
            f"{detected} is not an accepted image format. "
            f"Accepted: {', '.join(settings.MEDIA_ALLOWED_IMAGE_FORMATS)}."
        )

    try:
        # ⚠️ Orientation applied, then all EXIF dropped (❓Q6, P3) — `imaging.sanitize()` owns that
        # ordering trap. These are the only bytes that ever reach storage.
        rendered = imaging.sanitize(raw, quality=settings.MEDIA_IMAGE_QUALITY)
    except imaging.UnreadableImage as exc:
        _reject_undecodable(exc, owner=owner, declared=upload.content_type)

    with transaction.atomic():
        media = Media(
            owner=owner,
            state=MediaState.UPLOADED,
            image_format=rendered.image_format,
            byte_size=len(rendered.content),
            width=rendered.width,
            height=rendered.height,
        )
        # ⚠️ `save=False` then one explicit `save()`: `FieldFile.save()` defaults to writing the row
        # itself, which would insert with `state=uploaded` and leave a stranded row if the
        # `PROCESSING` follow-up failed. The name is the extension the *detected* format chose —
        # `_upload_path()` builds the real key from the pk.
        media.file.save(rendered.extension, ContentFile(rendered.content), save=False)
        media.save()

        media.state = MediaState.PROCESSING
        # ⚠️ `updated_at` must be listed — it is `auto_now`, and `update_fields` writes only the
        # named columns, so omitting it leaves the row reading as never-modified (the T2.2 note).
        media.save(update_fields=["state", "updated_at"])

        # ⚠️ Only the id crosses the broker, as a `str`, bound to a local before the closure is
        # built. Capturing `media` would put the owner id and the storage key in Redis (NFR-12) and
        # let the worker act on a pre-commit snapshot instead of re-reading the committed row.
        media_id = str(media.pk)
        transaction.on_commit(lambda: process_media.delay(media_id))

    logger.info(
        "media.uploaded",
        media_id=str(media.pk),
        # ⚠️ No filename and no storage key. An uploaded name routinely carries PII
        # (`IMG_outside_my_house.jpg`), and the key is a component of a presigned URL (NFR-12).
        image_format=rendered.image_format,
        byte_size=len(rendered.content),
    )
    return media


def resolve_media_for_attachment(*, owner: User, media_ids: Sequence[UUID | str]) -> int:
    """Validate `mediaIds` and return the count BR-3 needs (T2.6, API §6.3).

    ⚠️ **Every id must resolve, or the whole submission is refused.** Silently dropping an
    unresolvable id would let a photo-only submission fail BR-3 with "describe the problem" — an
    error about `description` when the client did attach a photo, and no way to act on it. That is
    the failure T2.2 refused when it declared `mediaIds` rather than letting DRF drop the key.

    ⚠️ **One message for "not yours", "already attached" and "does not exist".** Three distinct
    messages would turn this field into an oracle for whether a media id exists and who owns it —
    the "`403` to act, `404` to see" rule from T1.5, applied at field level.

    ⚠️ **A still-`PROCESSING` photo counts.** Arch §4.1 says "a Report can be usable before its
    Media is `Ready`"; requiring `READY` would make BR-3 depend on worker latency, so a citizen who
    submits a second after uploading would be told their photo does not exist.

    ⚠️ **Duplicates are collapsed, not rejected.** A client that sends the same id twice is buggy,
    not abusive, and `["a", "a"]` counting as two photos would let one upload satisfy a future
    minimum. `dict.fromkeys` rather than `set` so the length check below sees a stable count.
    """
    unique = list(dict.fromkeys(str(value) for value in media_ids))
    if not unique:
        return 0

    limit = settings.MEDIA_MAX_PER_REPORT
    if len(unique) > limit:
        raise MediaValidationError(
            {"media_ids": f"A report may carry at most {limit} photos."},
            code="VALIDATION_FAILED",
        )

    usable = (
        Media.objects.filter(pk__in=unique, owner=owner, report__isnull=True)
        .exclude(state=MediaState.REMOVED)
        .count()
    )
    if usable != len(unique):
        raise MediaValidationError(
            {"media_ids": "One or more photos are unavailable. Upload them again."},
            code="VALIDATION_FAILED",
        )
    return usable


def attach_media(*, report: Report, owner: User, media_ids: Sequence[UUID | str]) -> int:
    """Bind pre-uploaded photos to a Report, returning how many were bound (T2.6, API §6.3).

    ⚠️ **Two steps, not one: `resolve_media_for_attachment()` proves, this binds.** BR-3 is
    validated *before* the report row exists — a photo-only submission is only legal if the photos
    are real — so the count has to be established first, and the binding cannot happen until there
    is a report to bind to. Merging them would mean either creating the report on unverified ids or
    validating BR-3 after the write.

    ⚠️ **Ownership and `report IS NULL` are re-applied here, not assumed from the resolve step.**
    This is the write, and an authorization check that only runs on the read path is not an
    authorization check (FR-3). The null filter is also what makes attachment single-use: without
    it a citizen could pass one of their earlier reports' `mediaIds` and mount the same photo on ten
    reports, which FR-16 would then count as ten corroborating voices for one pothole.

    ⚠️ **No `transaction.atomic` of its own — it runs inside `submit_report()`'s.** A nested
    decorator here would create a savepoint that commits independently of the report write, so a
    later failure could leave photos attached to a report that was rolled back.
    """
    if not media_ids:
        return 0
    unique = [str(value) for value in media_ids]
    return (
        Media.objects.filter(pk__in=unique, owner=owner, report__isnull=True)
        .exclude(state=MediaState.REMOVED)
        .update(report=report)
    )


def remove_media(*, actor: User, media: Media) -> None:
    """`DELETE /media/{id}` — author pre-triage, or Admin moderation (API §6.4, FR-31).

    ⚠️ **A state change, never a row delete** (database.md "No hard deletes"). The read must be able
    to answer `410 GONE` afterwards, and only a surviving row can say that; a deleted row answers
    `404`, which tells a client to keep retrying and erases the fact that moderation acted.

    ⚠️ **The two authorizations are not interchangeable.** An author may only remove from a report
    that is still editable — `409 NOT_EDITABLE` once triage has run, the same lock T2.8 applies to
    the report itself, because a photo an Authority has already been dispatched on must not vanish
    from underneath them. An Admin removing under FR-31 is *moderation* and is deliberately not
    bound by that lock; acting on content after it has been seen is the entire point.

    ⚠️ **The `403` names neither role nor resource** (T1.5). "Only the author or an admin may do
    this" tells an attacker exactly which account is worth compromising.

    ⚠️ **Idempotent: removing an already-removed photo is a `204`, not a `409`.** The client's
    intent is already satisfied, and a conflict here would make a retried `DELETE` — the most
    ordinary thing a flaky mobile connection produces — look like a failure.

    ⚠️ **The stored objects stay in the bucket.** FR-31 is moderation, not erasure, and a removal
    may be reviewed or reversed; deleting the `FieldFile` here would make that irreversible and
    would run outside the transaction, so a rollback would leave a live row pointing at nothing.
    """
    is_admin = actor.role == Role.ADMIN
    is_author = media.owner_id == actor.pk

    if not (is_admin or is_author):
        raise PermissionDenied("You may not remove this photo.")

    if is_author and not is_admin:
        report = media.report
        if report is not None and not report.is_editable:
            raise Conflict(
                "This report has been triaged and can no longer be edited.",
                code="NOT_EDITABLE",
            )

    if media.state == MediaState.REMOVED:
        return

    media.state = MediaState.REMOVED
    media.save(update_fields=["state", "updated_at"])

    logger.info(
        "media.removed",
        media_id=str(media.pk),
        # BR-25's audit is a structured log line, knowingly — the immutable table is T8.1, which
        # replaces `_audit_privileged_action()`'s body. ⚠️ Do not add a second audit call path here.
        by_moderation=is_admin and not is_author,
    )
