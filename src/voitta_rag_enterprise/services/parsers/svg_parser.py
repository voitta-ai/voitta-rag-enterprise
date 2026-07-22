"""SVG → PNG → image embedding.

SigLIP can't ingest SVG (it expects raster); without this parser SVG files
land in the ``unsupported`` state. We rasterize via PyMuPDF and feed the
resulting PNG into the standard image flow as if the file had been a PNG to
start with.

Why PyMuPDF and not cairosvg: cairosvg dlopen()s the native libcairo at
import time. The packaged desktop app bundles no libcairo and macOS ships
none, so on end-user machines the import blew up (OSError, not ImportError —
it sailed past the old import guard) and EVERY .svg parked as ``error``.
PyMuPDF is already a direct dependency with MuPDF statically linked — no
external native library to forget.

Storage choice — we keep the **rasterized** PNG bytes in CAS, not the
original SVG source. The /api/images endpoint serves whatever is in CAS, so
clients get something they can render without a vector renderer; the
original .svg is still on disk if anyone needs it.

The output width is fixed at 512 px regardless of the SVG's intrinsic size
(SVG units are unbounded — a 10000x10000 raster would just bloat the CAS
for no embedding benefit, since SigLIP downscales to 224 internally).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from .base import (
    BaseParser,
    ExtractedImage,
    ParserResult,
    UnsupportedDocumentError,
)

logger = logging.getLogger(__name__)

_RASTER_WIDTH = 512  # px; aspect ratio preserved via the zoom matrix


class SvgParser(BaseParser):
    extensions: ClassVar[list[str]] = [".svg"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            svg_bytes = file_path.read_bytes()
        except OSError as e:
            return ParserResult.failure(f"svg read failed: {e}")

        # An empty .svg is a committed placeholder, not an indexer failure —
        # park it as unsupported (same reasoning as the empty-image case).
        if not svg_bytes.strip():
            raise UnsupportedDocumentError("empty svg file")

        try:
            import pymupdf
        except (ImportError, OSError) as e:
            # OSError included deliberately: a rasterizer whose native
            # payload can't load must degrade to a clean per-file failure,
            # not crash the pipeline (learned from cairosvg/libcairo).
            return ParserResult.failure(f"svg rasterizer unavailable: {e}")

        # Callers hold _EXTRACT_LOCK, which also serialises PyMuPDF use in
        # the PDF parser — no new concurrency surface here.
        try:
            with pymupdf.open(stream=svg_bytes, filetype="svg") as doc:
                page = doc[0]
                if page.rect.width <= 0:
                    # A zero-dimension SVG (e.g. PrusaSlicer commits a
                    # ``<svg width="0" height="0" viewBox="0 0 0 0"/>``
                    # placeholder) has nothing to rasterize. Deterministic —
                    # fails identically every run — so park it as unsupported
                    # rather than counting it as an error.
                    raise UnsupportedDocumentError(
                        "svg has no renderable content (zero width)"
                    )
                zoom = _RASTER_WIDTH / page.rect.width
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                png_bytes = pix.tobytes("png")
                width, height = pix.width, pix.height
        except UnsupportedDocumentError:
            # Deterministic "nothing to render" — let it propagate so the
            # extractor parks the file as unsupported, not error. Must precede
            # the broad handler below, which would otherwise swallow it.
            raise
        except Exception as e:
            return ParserResult.failure(f"svg rasterize failed: {e}")
        if not png_bytes:
            return ParserResult.failure("svg rasterizer produced no output")

        return ParserResult(
            content="",
            images=[
                ExtractedImage(
                    bytes=png_bytes,
                    mime="image/png",
                    position=0,
                    page=None,
                    width=width,
                    height=height,
                )
            ],
            metadata={
                "source_format": "svg",
                "rasterizer": "pymupdf",
                "raster_width": _RASTER_WIDTH,
            },
        )
