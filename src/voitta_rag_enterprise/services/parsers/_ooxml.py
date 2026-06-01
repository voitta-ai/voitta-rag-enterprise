"""Shared helpers for harvesting embedded media from OOXML packages.

``.docx`` / ``.xlsx`` / ``.pptx`` are Zip (OPC) packages; every embedded raster
lives under a ``*/media/`` folder regardless of how it is referenced — inline,
floating/anchored, inside a table, in a header/footer, or floating over a
worksheet. Scanning the package catches them all, where the document-model walk
(python-docx paragraph iteration, openpyxl) misses anchored, header/footer,
table, and sheet-floating images.

Used by ``docx_parser`` (to supplement its positioned inline walk) and
``xlsx_parser`` (its sole image source).
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# Raster formats SigLIP (via PIL) can embed. Vector parts (emf/wmf/svg) are
# skipped on purpose — the image encoder can't consume them and they would just
# fail downstream. (SVG files dropped into a folder go through the SVG parser,
# which rasterises them; that path is unaffected.)
_RASTER_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}

# Skip decorative glyphs (bullet dots, rule lines, 1px spacers) but keep real
# pictures. Applied only to the package sweep — callers that already know an
# image is intentional content (e.g. an inline figure) need not filter.
MIN_IMG_DIM = 32


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """``(width, height)`` via PIL, or ``(None, None)`` if undecodable."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return (None, None)


def iter_package_media(
    path: Path,
    *,
    media_prefix: str,
    skip_names: set[str] | None = None,
) -> Iterator[tuple[str, bytes, str]]:
    """Yield ``(entry_name, blob, mime)`` for raster media in an OOXML package.

    ``media_prefix`` is the OPC folder to scan, e.g. ``"word/media/"`` or
    ``"xl/media/"``. Non-raster parts and any entry in ``skip_names`` (zip entry
    names already handled by a positioned walk) are skipped. Robust to a
    non-zip / corrupt file — logs and yields nothing.
    """
    skip = skip_names or set()
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:  # pragma: no cover - defensive
        logger.debug("ooxml media scan: cannot open %s as zip: %s", path, e)
        return
    with zf:
        for name in zf.namelist():
            if not name.startswith(media_prefix) or name in skip:
                continue
            dot = name.rfind(".")
            mime = _RASTER_MIME.get(name[dot:].lower()) if dot >= 0 else None
            if mime is None:
                continue
            try:
                blob = zf.read(name)
            except (zipfile.BadZipFile, OSError) as e:  # pragma: no cover
                logger.debug("ooxml media scan: failed reading %s: %s", name, e)
                continue
            yield name, blob, mime
