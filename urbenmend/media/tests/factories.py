"""`factory_boy` factories and image fixtures for `media` (T2.4/T2.5).

⚠️ **The image builders here produce *real encoded bytes*, not `b"fake"` sentinels.** Every rule
this app enforces is a property of a decode — format detection, orientation, the EXIF strip, the
LANCZOS downscale — so a fixture Pillow cannot open tests nothing but the error path. `Image.new()`
is cheap enough that there is no reason to fake it.

[doc: testing.md "factory_boy"; API §6.4; FR-7, P3, BR-4]
"""

from __future__ import annotations

import io
from typing import Any

import factory
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from urbenmend.identity.tests.factories import UserFactory
from urbenmend.media.models import Media, MediaState

# EXIF tag 274 is `Orientation`; `6` means "rotate 90° CW to display upright". Named rather than
# inlined because `imaging.sanitize()`'s whole contract is what happens to this one number, and a
# bare `274` in an assertion reads as noise.
EXIF_ORIENTATION_TAG = 0x0112
EXIF_ORIENTATION_ROTATE_90 = 6


def image_bytes(
    *,
    width: int = 64,
    height: int = 48,
    image_format: str = "JPEG",
    color: tuple[int, int, int] = (200, 30, 30),
    exif: Image.Exif | None = None,
) -> bytes:
    """A real encoded image.

    ⚠️ **Non-square by default (64×48).** A square fixture cannot tell a correct
    `exif_transpose` from a no-op, and it cannot tell an aspect-preserving `thumbnail()` from a
    `resize()` to a fixed box — two of the three things T2.5 has to get right.
    """
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    if exif is not None:
        image.save(buffer, format=image_format, exif=exif)
    else:
        image.save(buffer, format=image_format)
    return buffer.getvalue()


def image_bytes_with_gps(*, width: int = 64, height: int = 48) -> bytes:
    """A JPEG carrying both an orientation tag and GPS coordinates.

    The exact shape of a phone photo BR-4/P3 are written about: the location the citizen's camera
    recorded, which is not the location the report is filed at, and which must never reach storage.

    ⚠️ **The GPS values are `IFDRational`, not `(numerator, denominator)` tuples.** Pillow *reads*
    rationals back as tuple-like objects, so the tuple form looks right — but `Exif.tobytes()`
    calls `abs()` on each value while writing and a plain tuple raises `TypeError` there. The
    fixture would then fail to build, which reads as a bug in `sanitize()`.
    """
    exif = Image.Exif()
    exif[EXIF_ORIENTATION_TAG] = EXIF_ORIENTATION_ROTATE_90
    # 0x8825 is the GPS IFD pointer; 1/2 are latitude ref + value, 3/4 longitude. Dhaka-ish.
    exif[0x8825] = {
        1: "N",
        2: (IFDRational(23), IFDRational(48), IFDRational(37)),
        3: "E",
        4: (IFDRational(90), IFDRational(24), IFDRational(45)),
    }
    return image_bytes(width=width, height=height, exif=exif)


def not_an_image() -> bytes:
    """Bytes no decoder will accept — the `422` fixture (API §6.4 "corrupt image")."""
    return b"%PDF-1.7\n% not an image at all\n"


class MediaFactory(factory.django.DjangoModelFactory[Media]):
    """One uploaded photo, **unattached and still `PROCESSING`** — what `POST /media` leaves behind.

    ⚠️ **Bypasses `upload_media()` on purpose**, the posture `ReportFactory` records: the service is
    the thing under test in `test_upload.py`, and routing fixtures through it would make one
    validation bug fail unrelated suites while also requiring real storage for every fixture.

    ⚠️ **`report` is null by default**, because that is the only state a photo can be attached
    from. A factory that defaulted to attached would make every T2.6 test start by undoing it, and
    `resolve_media_for_attachment()`'s single-use rule would look untestable.
    """

    class Meta:
        model = Media

    owner = factory.SubFactory(UserFactory)
    # ⚠️ `Any`: `ReadyMediaFactory` overrides this with a `SubFactory`, which is not a `None`.
    report: Any = None
    state = MediaState.PROCESSING
    # ⚠️ **`filename` is the extension alone, because that is what `upload_media()` passes.** It
    # reaches `_upload_path()` as the `filename` argument and is concatenated onto `"original"`, so
    # a full `"original.jpg"` here produces the key `reports/<id>/originaloriginal.jpg` — a fixture
    # whose storage key does not have production's shape, which is the one thing a fixture holding
    # a real file is for.
    file = factory.django.FileField(filename=".jpg", data=image_bytes())
    image_format = "JPEG"
    byte_size = 1024
    width = 64
    height = 48
    failure_reason = ""


class ReadyMediaFactory(MediaFactory):
    """A photo the worker has finished with — has a thumbnail, so `thumbnailUrl` is non-null."""

    state = MediaState.READY
    thumbnail = factory.django.FileField(filename=".jpg", data=image_bytes(width=32, height=24))
