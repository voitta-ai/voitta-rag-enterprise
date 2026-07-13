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

from .base import BaseParser, ExtractedImage, ParserResult

logger = logging.getLogger(__name__)

_RASTER_WIDTH = 512  # px; aspect ratio preserved via the zoom matrix


class SvgParser(BaseParser):
    extensions: ClassVar[list[str]] = [".svg"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            svg_bytes = file_path.read_bytes()
        except OSError as e:
            return ParserResult.failure(f"svg read failed: {e}")

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
                    return ParserResult.failure("svg has no width")
                zoom = _RASTER_WIDTH / page.rect.width
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                png_bytes = pix.tobytes("png")
                width, height = pix.width, pix.height
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
