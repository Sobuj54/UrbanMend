"""Pillow primitives: the EXIF strip, the orientation trap, the derivatives (T2.5).

These are the tests that make ❓Q6's answer real. Everything here reads *pixels and metadata*, never
a return code — a strip that silently does nothing passes any test that only checks for an
exception.

[doc: PRD P3, FR-7, BR-4; API §6.4; docs/08-coding-workflow.md §C3 "T2.5"]
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from urbenmend.media import imaging
from urbenmend.media.tests.factories import (
    EXIF_ORIENTATION_ROTATE_90,
    EXIF_ORIENTATION_TAG,
    image_bytes,
    image_bytes_with_gps,
    not_an_image,
)

# No `pytestmark = pytest.mark.django_db` — this module touches no database at all, which is the
# point of `imaging.py` being importable without Django set up.


def test_sanitize_removes_every_exif_tag_including_gps() -> None:
    """❓Q6, resolved: strip all EXIF always (P3, BR-4).

    ⚠️ Asserts on the *decoded output*, not on the function's return value. `_encode()` strips by
    re-encoding from pixels, so a regression that reintroduced `exif=` would still return a
    perfectly valid `RenderedImage` and only this assertion would notice.

    ⚠️ **The source is asserted to carry GPS *before* the strip runs.** Pillow silently drops EXIF
    it cannot serialize, so a fixture that quietly stopped writing the GPS IFD would make the rest
    of this test pass while proving nothing at all — the exact shape of a security test that has
    rotted into a no-op.
    """
    source = image_bytes_with_gps()
    source_exif = Image.open(io.BytesIO(source)).getexif()
    assert source_exif.get_ifd(0x8825), "fixture no longer carries GPS — this test proves nothing"

    rendered = imaging.sanitize(source, quality=82)

    reopened = Image.open(io.BytesIO(rendered.content))
    exif = reopened.getexif()

    assert dict(exif) == {}
    assert not exif.get_ifd(0x8825)  # the GPS IFD specifically
    assert not reopened.info.get("exif")


def test_sanitize_applies_orientation_before_stripping_it() -> None:
    """The §C3 trap: transpose *then* strip, never the other way round.

    Orientation `6` means "rotate 90° to display upright", so a 64×48 source must come out 48×64.
    Stripping first would produce a 64×48 output that is metadata-clean and permanently sideways —
    and would pass `test_sanitize_removes_every_exif_tag_including_gps` unchanged.
    """
    source = image_bytes(width=64, height=48, exif=_orientation_exif())

    rendered = imaging.sanitize(source, quality=82)

    assert (rendered.width, rendered.height) == (48, 64)


def _orientation_exif() -> Image.Exif:
    exif = Image.Exif()
    exif[EXIF_ORIENTATION_TAG] = EXIF_ORIENTATION_ROTATE_90
    return exif


def test_sanitize_leaves_an_untagged_image_the_way_up_it_was() -> None:
    """`exif_transpose` returning `None` must not become a rotation or a crash."""
    rendered = imaging.sanitize(image_bytes(width=64, height=48), quality=82)

    assert (rendered.width, rendered.height) == (64, 48)


def test_detect_format_reads_the_bytes_not_a_declared_type() -> None:
    assert imaging.detect_format(image_bytes(image_format="PNG")) == "PNG"
    assert imaging.detect_format(image_bytes(image_format="JPEG")) == "JPEG"


def test_detect_format_rejects_bytes_that_are_not_an_image() -> None:
    with pytest.raises(imaging.UnreadableImage):
        imaging.detect_format(not_an_image())


def test_detect_format_rejects_an_image_format_we_do_not_re_encode() -> None:
    """A real, decodable image in a format `_EXTENSIONS` has no entry for.

    Without this the allowlist check would sit only in `services.py`, and `_encode()` would raise a
    `KeyError` — a `500` — the first time a BMP reached it.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 0, 0)).save(buffer, format="BMP")

    with pytest.raises(imaging.UnreadableImage):
        imaging.detect_format(buffer.getvalue())


def test_a_truncated_image_is_refused_rather_than_opened() -> None:
    """`Image.open()` is lazy; `_open()` calls `load()` so the failure lands here, not in the worker."""
    whole = image_bytes(width=256, height=256)

    with pytest.raises(imaging.UnreadableImage):
        imaging.detect_format(whole[: len(whole) // 3])


def test_downscale_bounds_the_longest_edge_and_keeps_the_aspect_ratio() -> None:
    rendered = imaging.downscale(
        image_bytes(width=4000, height=3000), max_dimension=2048, quality=82
    )

    assert rendered.width == 2048
    assert rendered.height == 1536


def test_downscale_never_enlarges_an_image_that_already_fits() -> None:
    """`thumbnail()` is a no-op below the bound — a `resize()` would upscale and inflate the file."""
    rendered = imaging.downscale(image_bytes(width=64, height=48), max_dimension=2048, quality=82)

    assert (rendered.width, rendered.height) == (64, 48)


def test_thumbnail_is_bounded_by_its_own_smaller_dimension() -> None:
    rendered = imaging.thumbnail(image_bytes(width=4000, height=3000), dimension=320, quality=82)

    assert rendered.width == 320
    assert rendered.height == 240


def test_the_extension_follows_the_detected_format() -> None:
    """The storage key must describe the bytes, never the uploaded filename."""
    assert imaging.sanitize(image_bytes(image_format="PNG"), quality=82).extension == ".png"
    assert imaging.sanitize(image_bytes(image_format="JPEG"), quality=82).extension == ".jpg"
    assert imaging.sanitize(image_bytes(image_format="WEBP"), quality=82).extension == ".webp"


def test_a_transparent_png_flattens_onto_white_not_black() -> None:
    """The alpha trap: `convert("RGB")` alone keeps whatever sits under the transparency.

    Only reachable once something re-encodes a transparent source as JPEG. Asserted on a corner
    pixel because a bare convert produces a *black* rectangle — visually a corrupt upload, with no
    error anywhere to explain it.
    """
    source = io.BytesIO()
    Image.new("RGBA", (16, 16), (255, 0, 0, 0)).save(source, format="PNG")

    rendered = imaging._encode(
        Image.open(io.BytesIO(source.getvalue())), image_format="JPEG", quality=95
    )

    pixel = Image.open(io.BytesIO(rendered.content)).convert("RGB").getpixel((0, 0))
    # ⚠️ `isinstance`, not just `is not None`: `getpixel` is typed `float | tuple[int, ...]` because
    # a single-band image returns a scalar. The narrowing is what makes the iteration below legal,
    # and it also fails loudly if `_encode` ever stopped producing three channels.
    assert isinstance(pixel, tuple)
    assert all(channel > 240 for channel in pixel)
