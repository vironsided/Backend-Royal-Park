"""Safe normalization and storage compression for uploaded photographs."""

from __future__ import annotations

from io import BytesIO
import math

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # Adds HEIC/HEIF support when pillow-heif is installed.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - optional during lightweight local runs
    pass


class ImageConversionError(ValueError):
    """Raised when uploaded bytes cannot be decoded into a safe image."""


MAX_DECODED_IMAGE_PIXELS = 40_000_000


def image_to_webp(
    raw_bytes: bytes,
    *,
    max_dimension: int,
    quality: int = 82,
    min_quality: int = 52,
    target_max_bytes: int | None = None,
) -> bytes:
    """Decode, orient, resize and encode an uploaded image as WebP.

    Metadata is intentionally omitted. If a target size is supplied, quality is
    reduced first and dimensions second. The best valid WebP is still returned
    when an unusually noisy image cannot reach the requested target.
    """
    if not raw_bytes:
        raise ImageConversionError("Empty image")
    if max_dimension < 1:
        raise ValueError("max_dimension must be positive")

    quality = max(1, min(100, int(quality)))
    min_quality = max(1, min(quality, int(min_quality)))

    try:
        with Image.open(BytesIO(raw_bytes)) as source:
            if source.width * source.height > MAX_DECODED_IMAGE_PIXELS:
                raise ImageConversionError("Image dimensions are too large")
            source.load()
            image = ImageOps.exif_transpose(source)

            # Uploaded photographs never need animation. Preserve transparency
            # for PNG/WebP avatars and flatten every other mode to RGB.
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
            )

            best: bytes | None = None
            working = image
            for resize_attempt in range(5):
                for current_quality in range(quality, min_quality - 1, -6):
                    output = BytesIO()
                    working.save(
                        output,
                        format="WEBP",
                        quality=current_quality,
                        method=6,
                    )
                    candidate = output.getvalue()
                    if best is None or len(candidate) < len(best):
                        best = candidate
                    if target_max_bytes is None or len(candidate) <= target_max_bytes:
                        return candidate

                if target_max_bytes is None or not best:
                    break

                # Estimate the next dimensions from the byte ratio. Keep a
                # safety margin and a useful minimum resolution for evidence.
                ratio = math.sqrt(target_max_bytes / len(best)) * 0.92
                ratio = min(0.85, max(0.55, ratio))
                next_size = (
                    max(480, round(working.width * ratio)),
                    max(480, round(working.height * ratio)),
                )
                if next_size == working.size or (
                    working.width <= 480 and working.height <= 480
                ):
                    break
                working = working.copy()
                working.thumbnail(next_size, Image.Resampling.LANCZOS)

            if best:
                return best
    except ImageConversionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ImageConversionError("Invalid or unsupported image") from exc

    raise ImageConversionError("Invalid or unsupported image")
