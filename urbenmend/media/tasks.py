"""
Media — Celery tasks (T2.5).

The derivative half of the photo pipeline: downscale the stored master, produce a thumbnail, flip
the row to `READY`. The EXIF strip is **not** here — it runs synchronously in
`services.upload_media()`, before the first save, so nothing metadata-bearing is ever written to
storage (see that module's header for why the split falls where it does).

[doc: API §6.4; FR-7, P3; data-model §4; async-worker.md]
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

logger = structlog.get_logger(__name__)

# ⚠️ **Explicit name, not the module-path default.** Celery would otherwise register this as
# `urbenmend.media.tasks.process_media`, so moving or renaming the module renames the task — and any
# message already sitting in Redis under the old name fails on the worker as `NotRegistered`, after
# the deploy that caused it. The name is part of the wire contract between the API and the worker.
# `test_tasks.py` asserts it, because mypy cannot.
PROCESS_MEDIA_TASK = "media.process_media"


@shared_task(name=PROCESS_MEDIA_TASK)
def process_media(media_id: str, **_options: Any) -> None:
    """Compress and thumbnail one uploaded photo (FR-7, API §6.4).

    Args:
        media_id: The `Media.pk` as a string. ⚠️ **A string, not a `UUID` and not the instance.**
            kombu serializes a `UUID` happily but it arrives as a `str`, so the annotation would be
            a lie on the receiving side; passing the instance would put a whole row on the broker
            and let the worker act on a pre-commit snapshot.

    ⚠️ **Imports are inside the body.** `media/services.py` imports this module for the enqueue, so
    a module-level `from urbenmend.media.models import ...` closes the loop into a circular import
    — which surfaces at Django startup as an unrelated-looking `AttributeError` on a
    partially-initialised module. The same note `classification/tasks.py` carries.

    ⚠️ **The row is re-read and its state re-checked, never trusted from the id.** At-least-once
    delivery means this can run twice for one photo, and the photo may have been moderated
    (`REMOVED`) between enqueue and execution — regenerating derivatives for content an Admin has
    taken down would put it back in front of clients.

    ⚠️ **A failure marks the row `FAILED` and returns; it does not raise.** data-model §4 marks the
    branch "retry", and the retry is an operator action against a durable state, not an automatic
    redelivery: the common causes here are a permanently undecodable master or a storage
    misconfiguration, both of which a retry loop would hammer forever while the queue backs up
    behind it (O-2: the pipeline never blocks the queue). The report itself stays valid either way —
    BR-3 was satisfied at submission, and Arch §4.1 allows a Report to be usable before its Media is
    `Ready`.
    """
    from urbenmend.media import imaging
    from urbenmend.media.models import Media, MediaState

    try:
        media = Media.objects.get(pk=media_id)
    except Media.DoesNotExist:
        # Not an error worth raising: the row can legitimately be gone in a test teardown or a
        # restored-from-backup broker replaying an old message. Nothing to process, nothing to fix.
        logger.warning("media.process.missing", media_id=media_id)
        return

    if media.state not in {MediaState.UPLOADED, MediaState.PROCESSING}:
        logger.info("media.process.skipped", media_id=media_id, state=media.state)
        return

    try:
        with media.file.open("rb") as handle:
            raw = handle.read()

        downscaled = imaging.downscale(
            raw,
            max_dimension=settings.MEDIA_MAX_DIMENSION,
            quality=settings.MEDIA_IMAGE_QUALITY,
        )
        thumb = imaging.thumbnail(
            raw,
            dimension=settings.MEDIA_THUMBNAIL_DIMENSION,
            quality=settings.MEDIA_IMAGE_QUALITY,
        )
    except Exception as exc:  # noqa: BLE001 — see the docstring: any failure is a FAILED row.
        media.state = MediaState.FAILED
        # The operator-facing reason. It is never rendered to a client — §6.4's media resource has
        # no error field, and a decoder message can quote file contents (NFR-12).
        media.failure_reason = f"{type(exc).__name__}: {exc}"
        media.save(update_fields=["state", "failure_reason", "updated_at"])
        logger.exception("media.process.failed", media_id=media_id)
        return

    with transaction.atomic():
        # ⚠️ The master is **replaced**, not written alongside the original. `AWS_S3_FILE_OVERWRITE`
        # is `False`, so this lands on a new key and the pre-downscale object is left behind — that
        # is deliberate: it is already sanitized, and deleting it inside a transaction that may roll
        # back would leave a live row pointing at nothing. Bucket lifecycle rules reclaim it.
        media.file.save(downscaled.extension, ContentFile(downscaled.content), save=False)
        media.thumbnail.save(thumb.extension, ContentFile(thumb.content), save=False)
        media.image_format = downscaled.image_format
        media.byte_size = len(downscaled.content)
        media.width = downscaled.width
        media.height = downscaled.height
        media.state = MediaState.READY
        media.failure_reason = ""
        media.save()

    logger.info(
        "media.processed",
        media_id=media_id,
        width=downscaled.width,
        height=downscaled.height,
        byte_size=len(downscaled.content),
    )
