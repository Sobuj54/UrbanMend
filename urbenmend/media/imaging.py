"""
Media — Pillow primitives (T2.5).

Pure byte-in/byte-out functions: no Django models, no storage, no settings reads. Both the
synchronous sanitize (`services.upload_media`) and the asynchronous derivatives
(`tasks.process_media`) call in here, so the EXIF policy is written once and cannot differ between
the two paths.

⚠️ **❓Q6 RESOLVED (2026-08-08, by the project owner): strip all EXIF, always.** Orientation is
applied first, then the entire EXIF block goes — including GPS. There is no per-user setting and no
retain branch. BR-4 already makes the report's explicit coordinate authoritative, so photo GPS is
not merely private, it is *unused*: keeping it would create a second, unvalidated location for the
same report that nothing reads and P3 has to defend.

[doc: PRD P3, FR-7, BR-4; API §6.4; docs/08-coding-workflow.md §C3 "T2.5"]
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

# Pillow format name → file extension. This mapping is also the allowlist of formats this module
# will re-encode; `services.py` applies the *configured* allowlist on top of it (`415`).
# ⚠️ The extension is derived from the *detected* format, never from the uploaded filename, so a
# stored object's key always describes its actual bytes.
_EXTENSIONS: dict[str, str] = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


class UnreadableImage(Exception):
    """The bytes are not a decodable image, or not a format we re-encode.

    Raised for both "this is a PDF" and "this JPEG is truncated". `services.py` is what turns the
    distinction into `415` versus `422`, because only it knows the configured allowlist — this
    module stays a decoder wrapper with no policy in it.
    """


@dataclass(frozen=True)
class RenderedImage:
    """One encoded image plus the facts the `Media` row records about it."""

    content: bytes
    image_format: str
    extension: str
    width: int
    height: int


def _open(raw: bytes) -> Image.Image:
    """Decode, or raise `UnreadableImage`.

    ⚠️ **`Image.open()` is lazy, so it is not a validity check on its own.** It reads the header and
    stops; a file truncated halfway through its scan data opens without complaint and only fails
    later, inside whatever code touches a pixel. `load()` forces the full decode here, where the
    failure is still attributable to the upload rather than surfacing as a `500` from the worker.

    ⚠️ **`verify()` is deliberately not used instead.** It also forces a read, but Pillow documents
    that the image must be reopened afterwards — so a `verify()`-then-reuse would raise on the
    subsequent operation and invite someone to "fix" it by dropping the check.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        # ⚠️ The decoder's message is not propagated. Pillow quotes header bytes in some of its
        # errors, and this message reaches a client (§6.4's `422`); `services.py` logs the original
        # for operators instead (NFR-12).
        raise UnreadableImage("The image could not be decoded.") from exc
    return image


def _encode(image: Image.Image, *, image_format: str, quality: int) -> RenderedImage:
    """Re-encode from pixels, which is what actually drops the metadata.

    ⚠️ **A fresh `save()` from a decoded image is the strip.** There is no `Image.strip_exif()`, and
    deleting keys out of `image.info` would not help: `save()` re-emits whatever the plugin finds
    there plus anything the source carried. Writing the pixel buffer to a new buffer, passing no
    `exif=` and no `icc_profile=`, is the operation — the output has no EXIF because nothing ever
    put any in it.
    """
    buffer = io.BytesIO()
    prepared = image
    if image_format == "JPEG" and image.mode not in {"RGB", "L"}:
        # JPEG cannot hold alpha and Pillow raises rather than flattening. A PNG screenshot of a
        # blocked drain is a real submission, so it is flattened rather than refused.
        #
        # ⚠️ **Composited onto white, not `convert("RGB")`.** The bare convert discards the alpha
        # channel and keeps whatever RGB sits underneath it — which for a transparent PNG is
        # usually black, so a screenshot with a transparent background comes out as a black
        # rectangle. The failure looks like a corrupt upload and is unreachable from the pixels.
        if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            prepared = canvas
        else:
            prepared = image.convert("RGB")
    # `optimize=True` on every format: FR-7 asks for server-side compression, and it is lossless
    # extra work on the encoder's side, not extra quality loss.
    prepared.save(buffer, format=image_format, quality=quality, optimize=True)
    return RenderedImage(
        content=buffer.getvalue(),
        image_format=image_format,
        extension=_EXTENSIONS[image_format],
        width=prepared.width,
        height=prepared.height,
    )


def detect_format(raw: bytes) -> str:
    """The Pillow format name of these bytes, or raise `UnreadableImage`.

    ⚠️ **This, not the request's `Content-Type`.** The header is client-supplied: a `.php` renamed
    `.jpg` arrives as `image/jpeg`, and a legitimate photo from a misconfigured client arrives as
    `application/octet-stream`. Only the decoder knows, so the allowlist is checked against this.
    """
    image = _open(raw)
    if not image.format or image.format not in _EXTENSIONS:
        raise UnreadableImage("Unsupported image format.")
    return image.format


def sanitize(raw: bytes, *, quality: int) -> RenderedImage:
    """Apply EXIF orientation, then strip all metadata. The only bytes we ever store.

    ⚠️ **Order is load-bearing and it is the trap §C3 names: transpose *then* strip.** The
    orientation tag lives in EXIF, so stripping first discards the instruction that says which way
    is up — and the image is then stored rotated, permanently, with nothing left to correct it from.
    Every phone photo taken in portrait is affected, and the bug is invisible in any test that
    checks metadata rather than pixels.

    ⚠️ **`exif_transpose` returns a new image and may return `None`-equivalent behaviour on images
    with no tag**; it is called for its return value, never relied on to mutate in place.
    """
    image = _open(raw)
    image_format = image.format or ""
    if image_format not in _EXTENSIONS:
        raise UnreadableImage("Unsupported image format.")
    upright = ImageOps.exif_transpose(image) or image
    return _encode(upright, image_format=image_format, quality=quality)


def downscale(raw: bytes, *, max_dimension: int, quality: int) -> RenderedImage:
    """Bound the longest edge, re-encoding in place (FR-7 "server-side compression").

    ⚠️ **`thumbnail()` is a no-op when the image already fits**, so a small photo is not upscaled
    into a bigger file. It also preserves aspect ratio, which a `resize()` to a fixed box would not.

    No transpose here: `sanitize()` already made the stored master upright and metadata-free, and a
    second transpose on an image with no orientation tag is a silent no-op that reads as a
    safeguard.
    """
    image = _open(raw)
    image_format = image.format or ""
    if image_format not in _EXTENSIONS:
        raise UnreadableImage("Unsupported image format.")
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return _encode(image, image_format=image_format, quality=quality)


def thumbnail(raw: bytes, *, dimension: int, quality: int) -> RenderedImage:
    """The list/map-sized derivative (FR-7).

    Same operation as `downscale` at a smaller bound — kept as its own function because the two are
    driven by different settings and the caller reads better for it.
    """
    return downscale(raw, max_dimension=dimension, quality=quality)
