"""Image loading helpers: EXIF normalisation, HEIC, thumbnail cache.

The loader is the single place in the codebase that knows how to translate a
filesystem path into a Pillow ``Image`` with sensible defaults. Both the GUI
(thumbnails) and the renderer (full-resolution prints) go through here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Register HEIC/HEIF opener if available (system-installed libheif)
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover — optional
    logger.info("pillow-heif not available; HEIC files will not load")


SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".heic", ".heif", ".bmp"}
)


@dataclass(frozen=True)
class ImageMetadata:
    """Metadata extracted from a source file, EXIF-corrected."""

    path: Path
    width_px: int
    height_px: int
    exif_datetime: datetime | None


def is_supported(path: Path) -> bool:
    """True if the file's extension is among :data:`SUPPORTED_EXTENSIONS`."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def load_image_oriented(path: Path) -> Image.Image:
    """Open ``path`` with Pillow and apply EXIF orientation.

    Args:
        path: Image file.

    Returns:
        A Pillow ``Image`` with EXIF orientation baked into pixel data.

    Raises:
        OSError: If the file cannot be opened or decoded.
    """
    img = Image.open(path)
    return ImageOps.exif_transpose(img)


def read_metadata(path: Path) -> ImageMetadata:
    """Return :class:`ImageMetadata` without keeping the full image in memory.

    Lazy: opens the file, reads dimensions + EXIF, then closes.
    """
    with Image.open(path) as img:
        oriented = ImageOps.exif_transpose(img)
        w, h = oriented.size
        dt = _exif_datetime(oriented)
    return ImageMetadata(path=path, width_px=w, height_px=h, exif_datetime=dt)


def _exif_datetime(img: Image.Image) -> datetime | None:
    """Best-effort extraction of EXIF DateTimeOriginal."""
    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001 — Pillow can raise unstructured errors here
        return None
    if not exif:
        return None
    # Tag 36867 = DateTimeOriginal; 306 = DateTime
    raw = exif.get(36867) or exif.get(306)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=256)
def _thumbnail_cached(path_str: str, mtime_ns: int, max_side: int) -> bytes:
    """LRU-cached thumbnail bytes. ``mtime_ns`` invalidates on file change."""
    del mtime_ns  # only present to key the cache by file version
    img = load_image_oriented(Path(path_str))
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if img.mode not in {"RGB", "RGBA"}:
        img = img.convert("RGBA" if "A" in img.mode else "RGB")
    # Serialise to PNG bytes — easy to hand off to Gdk.Texture
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def thumbnail_bytes(path: Path, max_side: int = 256) -> bytes:
    """Return PNG-encoded thumbnail bytes, cached per (path, mtime, size)."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return _thumbnail_cached(str(path), mtime, max_side)


def downscale_for_dpi(img: Image.Image, target_w_mm: float, target_h_mm: float, dpi: int) -> Image.Image:
    """Downscale ``img`` so it has at most ``dpi`` resolution at the target size.

    Args:
        img: Source Pillow image.
        target_w_mm: Final printed width in millimetres.
        target_h_mm: Final printed height in millimetres.
        dpi: Target dots per inch (300 is good for photo printing).

    Returns:
        Either ``img`` unchanged (if already small enough) or a downscaled copy.
    """
    max_w = max(1, int(round(target_w_mm / 25.4 * dpi)))
    max_h = max(1, int(round(target_h_mm / 25.4 * dpi)))
    if img.width <= max_w and img.height <= max_h:
        return img
    out = img.copy()
    out.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    return out
