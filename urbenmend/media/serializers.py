"""
Media — request/response shapes for `/media` (T2.4).

⚠️ **The serializer validates *shape*; `services.py` enforces the *rules*** (FR-3, Arch §3.1). Size,
format and decodability all live in `upload_media()`, so a management command or a future bulk
importer gets the same three rejections the endpoint does.

[doc: API §6.4, §1.2, §4.1; FR-7, FR-31]
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import serializers

from urbenmend.api.serializers import CamelCaseSerializer
from urbenmend.media.models import MediaState

if TYPE_CHECKING:
    from urbenmend.media.models import Media


class MediaUploadSerializer(CamelCaseSerializer):
    """`POST /media` request body: `multipart/form-data` with `file` (API §6.4).

    ⚠️ **A bare `FileField`, with no `ImageField` and no `max_length`/size validator.** DRF's
    `ImageField` calls Django's image validation, which uses Pillow's `verify()` and answers a plain
    `400` — collapsing §6.4's `415` (wrong format) and `422` (corrupt) into one status with one
    message. The service raises the two separately because the client's remedy differs: send a JPEG,
    versus retake the photo.

    ⚠️ **No size validator here either**, for the matching reason: DRF would render it `400`, and
    §6.4 fixes `413`. The bound is `settings.MEDIA_MAX_UPLOAD_BYTES`, checked in `upload_media()`
    before anything is decoded.
    """

    file = serializers.FileField()


class MediaResponseSerializer(CamelCaseSerializer):
    """The media resource, for both `POST /media` (`202`) and `GET /media/{id}` (`200`).

    §6.4 shows two bodies that differ only in how much is populated —
    `{"id","state":"processing","thumbnailUrl":null}` on upload and
    `{"id","state":"ready","url","thumbnailUrl"}` on read. One serializer, because two would be free
    to drift and the difference between them is data, not shape.

    ⚠️ **`url` and `thumbnailUrl` are computed, never `FileField`s.** A DRF `FileField` renders
    `.url`, which raises `ValueError` on an empty file — so the `202` body, where the thumbnail does
    not exist yet, would be a `500` instead of §6.4's documented `null`.

    ⚠️ **Not a `ModelSerializer`.** The neighbouring columns are `owner`, `report`, `byte_size` and
    `failure_reason`: an owner id is another citizen's identifier, and `failure_reason` carries
    decoder text that can quote file contents (NFR-12). One `fields` edit — or one `"__all__"` —
    would publish all four. The same reasoning T2.2 recorded for `ReportSubmitSerializer`.
    """

    id = serializers.CharField(read_only=True)
    state = serializers.CharField(read_only=True)
    url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    def get_url(self, media: Media) -> str | None:
        """The presigned URL of the stored master, or `null` while it is unusable.

        ⚠️ **`null` once moderation has removed the photo**, even though `GET /media/{id}` answers
        `410` for that case and never reaches here. This serializer also renders inside `GET
        /reports/{id}`'s `media[]` (T2.7), where a removed row is filtered out by the selector — so
        this is the third layer, and the one that holds if either of the others is changed.
        """
        return self._file_url(media, "file")

    def get_thumbnail_url(self, media: Media) -> str | None:
        """`null` until the worker produces it — §6.4's `202` body shows exactly that."""
        return self._file_url(media, "thumbnail")

    @staticmethod
    def _file_url(media: Media, field: str) -> str | None:
        if media.state in {MediaState.HIDDEN, MediaState.REMOVED}:
            return None
        stored = getattr(media, field)
        if not stored:
            return None
        # `.url` on an S3 backend mints a presigned URL (`AWS_QUERYSTRING_AUTH`), so it is
        # short-lived by design (`AWS_QUERYSTRING_EXPIRE`) and must be generated per response
        # rather than cached on the row.
        return str(stored.url)
