"""`process_media` — the derivative half of the pipeline (T2.5).

The strip is not tested here; it is synchronous and lives in `test_upload.py`. What this module
covers is what the worker adds — the downscale, the thumbnail, `READY`, and the two ways the task
must decline to act.

[doc: API §6.4; FR-7; data-model §4 "Lifecycle"; async-worker.md; docs/08-coding-workflow.md §C3]
"""

from __future__ import annotations

import io

import pytest
from celery import current_app
from django.core.files.base import ContentFile
from PIL import Image

from urbenmend.media.models import Media, MediaState
from urbenmend.media.tasks import PROCESS_MEDIA_TASK, process_media
from urbenmend.media.tests.factories import MediaFactory, image_bytes, not_an_image

pytestmark = pytest.mark.django_db


def test_the_task_is_registered_under_its_explicit_name() -> None:
    """⚠️ The name is part of the wire contract between the API and the worker.

    Without `name=`, Celery registers this as `urbenmend.media.tasks.process_media` — so moving or
    renaming the module renames the task, and any message already sitting in Redis under the old
    name fails on the worker as `NotRegistered`, *after* the deploy that caused it. mypy cannot see
    this, which is why it is asserted.
    """
    assert PROCESS_MEDIA_TASK == "media.process_media"
    assert process_media.name == PROCESS_MEDIA_TASK
    assert PROCESS_MEDIA_TASK in current_app.tasks


def _media_with(content: bytes, **kwargs: object) -> Media:
    """A row whose stored master really is `content` — the worker re-reads it from storage."""
    media = MediaFactory.create(**kwargs)
    media.file.save(".jpg", ContentFile(content), save=True)
    return media


def test_the_master_is_downscaled_and_a_thumbnail_is_produced(settings) -> None:  # type: ignore[no-untyped-def]
    settings.MEDIA_MAX_DIMENSION = 512
    settings.MEDIA_THUMBNAIL_DIMENSION = 64
    media = _media_with(image_bytes(width=2000, height=1000))

    process_media(str(media.pk))

    media.refresh_from_db()
    assert media.state == MediaState.READY
    assert (media.width, media.height) == (512, 256)
    assert media.thumbnail

    with media.thumbnail.open("rb") as handle:
        thumb = Image.open(io.BytesIO(handle.read()))
    assert (thumb.width, thumb.height) == (64, 32)


def test_the_recorded_dimensions_and_size_describe_the_downscaled_master(settings) -> None:  # type: ignore[no-untyped-def]
    """The row must describe the bytes it points at, not the bytes that were uploaded.

    ⚠️ A worker that saved the derivative but left `width`/`height`/`byte_size` at the upload values
    would leave every client laying out images at the wrong aspect and size, with nothing in the
    response to reveal the mismatch.
    """
    settings.MEDIA_MAX_DIMENSION = 100
    media = _media_with(image_bytes(width=800, height=600))
    stale_size = media.byte_size

    process_media(str(media.pk))

    media.refresh_from_db()
    assert (media.width, media.height) == (100, 75)
    assert media.byte_size != stale_size
    with media.file.open("rb") as handle:
        stored = Image.open(io.BytesIO(handle.read()))
    assert (stored.width, stored.height) == (100, 75)


def test_a_small_photo_is_not_enlarged(settings) -> None:  # type: ignore[no-untyped-def]
    """`thumbnail()` is a no-op below the bound — a `resize()` would upscale and inflate the file."""
    settings.MEDIA_MAX_DIMENSION = 2048
    media = _media_with(image_bytes(width=64, height=48))

    process_media(str(media.pk))

    media.refresh_from_db()
    assert (media.width, media.height) == (64, 48)


def test_a_second_delivery_is_a_no_op_rather_than_a_second_downscale() -> None:
    """⚠️ At-least-once delivery means this runs twice for one photo, and it must be safe.

    The guard is the state check, not idempotent maths: re-running the pipeline on an
    already-downscaled master would compress it a second time, so a photo redelivered three times
    would visibly degrade with nothing to explain why.
    """
    media = _media_with(image_bytes(width=800, height=600))
    process_media(str(media.pk))
    media.refresh_from_db()
    first_size = media.byte_size

    process_media(str(media.pk))

    media.refresh_from_db()
    assert media.byte_size == first_size


def test_a_photo_removed_between_enqueue_and_execution_is_left_alone() -> None:
    """⚠️ Moderation wins the race (FR-31).

    The enqueue happens at upload; an Admin can remove the photo before the worker reaches it.
    Regenerating derivatives for content that has been taken down would put it back in front of
    clients — and would flip `REMOVED` to `READY`, undoing the moderation silently.
    """
    media = _media_with(image_bytes(), state=MediaState.REMOVED)

    process_media(str(media.pk))

    media.refresh_from_db()
    assert media.state == MediaState.REMOVED
    assert not media.thumbnail


def test_an_undecodable_master_marks_the_row_failed_rather_than_raising() -> None:
    """data-model §4 marks the branch "retry" — a durable state an operator acts on, not a redelivery.

    ⚠️ **`FAILED` is not a delete.** A row that vanished on a storage error would take its Report's
    BR-3 justification with it: a report that was valid at submission would retroactively have no
    photo and no adequate description.

    ⚠️ **It does not raise**, because raising means Celery retries — and the common causes here (a
    permanently undecodable master, a storage misconfiguration) would be hammered forever while the
    queue backs up behind them (O-2: the pipeline never blocks the queue).
    """
    media = _media_with(not_an_image())

    process_media(str(media.pk))  # must not raise

    media.refresh_from_db()
    assert media.state == MediaState.FAILED
    assert media.failure_reason
    assert not media.thumbnail


def test_a_failure_reason_is_recorded_for_operators_and_never_served() -> None:
    """⚠️ §6.4's media resource has no error field, and a decoder message can quote file contents.

    Asserted against the serializer rather than the endpoint so the rule holds for every caller of
    it — including T2.7's `media[]` inside `GET /reports/{id}`.
    """
    from urbenmend.media.serializers import MediaResponseSerializer

    media = _media_with(not_an_image())
    process_media(str(media.pk))
    media.refresh_from_db()

    rendered = MediaResponseSerializer(media).data

    assert "failureReason" not in rendered
    assert "failure_reason" not in rendered


def test_a_missing_row_is_logged_and_returns() -> None:
    """A broker replaying an old message, or a row gone in teardown. Nothing to process, not a 500."""
    process_media("00000000-0000-0000-0000-000000000000")  # must not raise


def test_a_failed_row_is_not_reprocessed_by_a_redelivery() -> None:
    """`FAILED` is terminal for the worker: retry is an operator action against a durable state.

    ⚠️ If `FAILED` were retryable here, an undecodable master would cycle
    `FAILED → process → FAILED` on every redelivery — the queue-blocking loop O-2 rules out.
    """
    media = _media_with(not_an_image())
    process_media(str(media.pk))
    media.refresh_from_db()
    assert media.state == MediaState.FAILED

    media.failure_reason = ""
    media.save(update_fields=["failure_reason", "updated_at"])
    process_media(str(media.pk))

    media.refresh_from_db()
    assert media.state == MediaState.FAILED
    assert media.failure_reason == ""  # untouched: the task declined to run at all


def test_the_report_link_survives_processing() -> None:
    """The worker touches derivatives, never the attachment T2.6 made.

    `media.save()` here is a full save, so a stale in-memory `report_id` would overwrite the FK a
    concurrent `submit_report()` had just set — the reason the task re-reads the row rather than
    trusting anything from the enqueue.
    """
    from urbenmend.reporting.tests.factories import ReportFactory

    report = ReportFactory.create()
    media = _media_with(image_bytes(), report=report, owner=report.author)

    process_media(str(media.pk))

    media.refresh_from_db()
    assert media.report_id == report.pk
    assert media.state == MediaState.READY
