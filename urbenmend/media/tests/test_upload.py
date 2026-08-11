"""`POST /media` — the upload contract (T2.4, API §6.4).

The four rejections (`413`, `415`, `422`, `403`) and the one acceptance. Everything here goes
through the HTTP layer, because the statuses *are* the contract and a service-level test would pass
while DRF rewrote one of them.

[doc: API §6.4, §4.1; FR-7, P3, BR-4; docs/08-coding-workflow.md §C3 "T2.4"]
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image

from urbenmend.identity.models import Role, User
from urbenmend.identity.tests.factories import UserFactory
from urbenmend.media.models import Media, MediaState
from urbenmend.media.tests.factories import image_bytes, image_bytes_with_gps, not_an_image

pytestmark = pytest.mark.django_db

# ⚠️ Patched at the **use** site, not where the task is defined — `media/services.py` imported the
# function object, so patching `urbenmend.media.tasks.process_media.delay` would leave the service
# holding the original and every enqueue assertion below would read the real Celery client. The
# T2.2 rule; `test_submission.py` patches its own task the same way.
DELAY = "urbenmend.media.services.process_media.delay"


def _url() -> str:
    return reverse("api:media")


def _signed_in(role: str = Role.CITIZEN) -> tuple[Client, User]:
    user = UserFactory.create(role=role)
    client = Client()
    client.force_login(user)
    return client, user


def _upload(
    content: bytes, *, name: str = "photo.jpg", content_type: str = "image/jpeg"
) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type=content_type)


def test_a_photo_is_accepted_and_the_row_records_the_decoded_facts() -> None:
    """§6.4's `202`: `{id, state: "processing", thumbnailUrl: null}`."""
    client, user = _signed_in()

    with patch(DELAY):
        response = client.post(_url(), {"file": _upload(image_bytes(width=64, height=48))})

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == MediaState.PROCESSING
    # ⚠️ `null`, not omitted and not a `500`. `thumbnailUrl` is a `SerializerMethodField` precisely
    # because a DRF `FileField` raises `ValueError` on the empty thumbnail this state always has.
    assert body["thumbnailUrl"] is None
    assert body["url"]

    media = Media.objects.get(pk=body["id"])
    assert media.owner_id == user.pk
    # Unattached — §6.4 hands back a handle, §6.3 binds it. The window is the contract (T2.6).
    assert media.report_id is None
    assert media.image_format == "JPEG"
    assert (media.width, media.height) == (64, 48)
    assert media.byte_size > 0


def test_the_response_publishes_no_owner_and_no_failure_reason() -> None:
    """⚠️ `MediaResponseSerializer` is not a `ModelSerializer`, and this is why.

    The neighbouring columns are `owner`, `report`, `byte_size` and `failure_reason`: an owner id is
    another citizen's identifier and `failure_reason` carries decoder text that can quote file
    contents (NFR-12). One `"__all__"` would publish all four.
    """
    client, _ = _signed_in()

    with patch(DELAY):
        response = client.post(_url(), {"file": _upload(image_bytes())})

    assert set(response.json()) == {"id", "state", "url", "thumbnailUrl"}


def test_the_stored_bytes_carry_no_exif_at_all() -> None:
    """❓Q6 end-to-end: nothing EXIF-bearing ever reaches storage (P3, BR-4, §C3).

    ⚠️ **Read back out of storage, not out of the response.** `test_imaging.py` proves `sanitize()`
    strips; this proves the *stored object* is what `sanitize()` returned. The bug it exists to
    catch is an `upload_media()` that saves `upload.read()` and defers the strip to the worker —
    which would satisfy §6.4's prose ("strips EXIF asynchronously") and still leave a GPS-tagged
    photo of a citizen's home in the bucket for as long as the queue is deep.
    """
    client, _ = _signed_in()

    with patch(DELAY):
        response = client.post(_url(), {"file": _upload(image_bytes_with_gps())})

    assert response.status_code == 202
    media = Media.objects.get(pk=response.json()["id"])
    with media.file.open("rb") as handle:
        exif = Image.open(handle).getexif()

    assert dict(exif) == {}
    assert not exif.get_ifd(0x8825)  # the GPS IFD specifically


def test_the_orientation_tag_is_applied_before_it_is_stripped() -> None:
    """The §C3 ordering trap, through the endpoint: a 64×48 source with orientation `6` is recorded
    48×64. Strip-then-transpose stores a permanently sideways photo and passes the EXIF test above
    unchanged."""
    client, _ = _signed_in()

    with patch(DELAY):
        response = client.post(_url(), {"file": _upload(image_bytes_with_gps(width=64, height=48))})

    media = Media.objects.get(pk=response.json()["id"])
    assert (media.width, media.height) == (48, 64)


def test_the_derivative_task_is_not_published_before_the_transaction_commits() -> None:
    """⚠️ Asserted **from the failing side** — the T2.2 rule, and the whole point of `on_commit`.

    The mock must NOT have been called when the response returns. An inline `.delay()` passes a
    "was it called" test and lets an idle worker `SELECT` the row before it commits, find nothing,
    and leave the photo without a thumbnail forever — load-dependent, so it survives every local run.
    """
    client, _ = _signed_in()

    with patch(DELAY) as delay:
        response = client.post(_url(), {"file": _upload(image_bytes())})

    assert response.status_code == 202
    delay.assert_not_called()


def test_the_derivative_task_is_published_once_the_transaction_commits() -> None:
    """The other half: without it, `on_commit(lambda: None)` — or no enqueue at all — would pass.

    ⚠️ **Only the id crosses the broker, as a `str`** (NFR-12). Capturing the instance would put the
    owner id and the storage key in Redis and let the worker act on a pre-commit snapshot.
    """
    client, _ = _signed_in()

    with patch(DELAY) as delay, TestCase.captureOnCommitCallbacks(execute=True):
        response = client.post(_url(), {"file": _upload(image_bytes())})

    delay.assert_called_once_with(response.json()["id"])
    (sent,) = delay.call_args.args
    assert isinstance(sent, str)


def test_the_row_is_left_processing_for_the_worker_to_pick_up() -> None:
    """`UPLOADED → PROCESSING` is a second UPDATE in the same transaction (the T2.2 shape).

    ⚠️ `updated_at` must be in that `update_fields` list — it is `auto_now`, and `update_fields`
    writes only the named columns, so omitting it leaves the row reading as never-modified.
    """
    client, _ = _signed_in()

    with patch(DELAY):
        response = client.post(_url(), {"file": _upload(image_bytes())})

    media = Media.objects.get(pk=response.json()["id"])
    assert media.state == MediaState.PROCESSING
    assert media.updated_at >= media.created_at


def test_a_file_over_the_size_limit_is_413_and_nothing_is_stored(settings) -> None:  # type: ignore[no-untyped-def]
    """§6.4's `413 PAYLOAD_TOO_LARGE`.

    ⚠️ **Checked before any decode**, which is why the fixture can be junk bytes rather than a real
    oversized image: reversed, a 500 MB file would be fully decoded before being refused for its
    size — the shape of an upload-based DoS, on an endpoint that ships unthrottled until T2.9.

    ⚠️ Also the only bound there is: Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` explicitly exempts
    `request.FILES`, so without this check a file upload has no framework-level size limit at all.
    """
    settings.MEDIA_MAX_UPLOAD_BYTES = 64
    client, _ = _signed_in()

    response = client.post(_url(), {"file": _upload(b"x" * 65)})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert not Media.objects.exists()


def test_a_real_image_in_a_disallowed_format_is_415(settings) -> None:  # type: ignore[no-untyped-def]
    """§6.4's `415` — a perfectly decodable image whose format is not on the allowlist."""
    settings.MEDIA_ALLOWED_IMAGE_FORMATS = ["JPEG"]
    client, _ = _signed_in()

    response = client.post(_url(), {"file": _upload(image_bytes(image_format="PNG"), name="x.png")})

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert not Media.objects.exists()


def test_bytes_that_are_not_an_image_are_422_not_415() -> None:
    """§6.4's `422` — and the distinction from `415` is the point.

    `415` means "send a JPEG instead of this"; `422` means "this file is damaged, take the photo
    again". One code for both leaves a client unable to tell which remedy applies.
    """
    client, _ = _signed_in()

    response = client.post(
        _url(),
        {"file": _upload(not_an_image(), name="x.pdf", content_type="application/pdf")},
    )

    assert response.status_code == 422
    assert not Media.objects.exists()


def test_the_client_declared_content_type_gets_nothing_past_the_decoder() -> None:
    """⚠️ The allowlist is checked against what Pillow **detects**, never the request header.

    A renamed executable arrives with a perfectly respectable `image/jpeg` on it. If
    `upload.content_type` were trusted anywhere in the path, this is the request that would store it.
    """
    client, _ = _signed_in()

    response = client.post(
        _url(),
        {"file": _upload(b"MZ\x90\x00 not a photo", name="photo.jpg", content_type="image/jpeg")},
    )

    assert response.status_code == 422
    assert not Media.objects.exists()


def test_a_truncated_photo_is_refused_rather_than_stored_half_decoded() -> None:
    """`Image.open()` is lazy, so this is only refused because `imaging._open()` calls `load()`.

    Without that, the upload would be stored and the failure would surface later as a `500` from the
    worker — attributed to the pipeline instead of to the request that caused it.
    """
    whole = image_bytes(width=256, height=256)
    client, _ = _signed_in()

    response = client.post(_url(), {"file": _upload(whole[: len(whole) // 3])})

    assert response.status_code == 422
    assert not Media.objects.exists()


def test_the_rejection_message_does_not_echo_the_decoder() -> None:
    """⚠️ Pillow quotes header bytes in some of its errors, and this message reaches a client.

    A crafted upload could otherwise echo chosen content back out through the §4.1 envelope.
    Operators get the real decoder text in the log line instead, correlated by `traceId` (NFR-12).
    """
    client, _ = _signed_in()

    response = client.post(_url(), {"file": _upload(b"%PDF-1.7 secret-marker-abc123")})

    assert response.status_code == 422
    assert "secret-marker" not in response.content.decode()


def test_an_empty_file_part_is_400_not_422() -> None:
    """A broken client, not a corrupt image — "take the photo again" would be the wrong instruction.

    ⚠️ DRF's own `FileField` rejects an empty upload before the service sees it, so this asserts the
    *status* rather than the service's message; either layer answering `400` satisfies §6.4.
    """
    client, _ = _signed_in()

    response = client.post(_url(), {"file": _upload(b"", name="empty.jpg")})

    assert response.status_code == 400
    assert not Media.objects.exists()


def test_a_missing_file_field_is_400() -> None:
    client, _ = _signed_in()

    response = client.post(_url(), {})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_an_anonymous_upload_is_401() -> None:
    """⚠️ `401`, not DRF's default `403` — the rewrite `urbenmend_exception_handler` undoes globally."""
    response = Client().post(_url(), {"file": _upload(image_bytes())})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("role", [Role.AUTHORITY, Role.ADMIN])
def test_a_non_citizen_may_not_upload(role: str) -> None:
    """§6.4 "Authorization: Citizen".

    ⚠️ Enforced in `upload_media()`, not by a permission class (FR-3) — so this also proves the
    service check is reached rather than a DRF class standing in for it. Admin is parametrized
    deliberately: Admins moderate photos, they do not file reports.
    """
    client, _ = _signed_in(role)

    response = client.post(_url(), {"file": _upload(image_bytes())})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert not Media.objects.exists()


def test_the_denial_names_neither_the_role_nor_the_resource() -> None:
    """T1.5's rule applied here: a message naming a role tells an attacker which account to take."""
    client, _ = _signed_in(Role.AUTHORITY)

    response = client.post(_url(), {"file": _upload(image_bytes())})

    message = response.json()["error"]["message"].lower()
    assert "authority" not in message
    assert "admin" not in message
