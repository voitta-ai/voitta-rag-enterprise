"""SVG → PNG → image embedding.

SigLIP can't ingest SVG (it expects raster); without this parser SVG files
land in the ``unsupported`` state. We rasterize via ``cairosvg`` and feed the
resulting PNG into the standard image flow as if the file had been a PNG to
start with.

Storage choice — we keep the **rasterized** PNG bytes in CAS, not the
original SVG source. The /api/images endpoint serves whatever is in CAS, so
clients get something they can render without a vector renderer; the
original .svg is still on disk if anyone needs it.

The output width is fixed at 512 px regardless of the SVG's intrinsic size
(SVG units are unbounded — a 10000x10000 raster would just bloat the CAS
for no embedding benefit, since SigLIP downscales to 224 internally).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import ClassVar

from PIL import Image as PILImage

from .base import BaseParser, ExtractedImage, ParserResult

logger = logging.getLogger(__name__)

_RASTER_WIDTH = 512  # px; aspect ratio preserved by cairosvg


class SvgParser(BaseParser):
    extensions: ClassVar[list[str]] = [".svg"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            svg_bytes = file_path.read_bytes()
        except OSError as e:
            return ParserResult.failure(f"svg read failed: {e}")

        try:
            import cairosvg
        except ImportError as e:
            return ParserResult.failure(f"cairosvg not installed: {e}")

        try:
            png_bytes = cairosvg.svg2png(
                bytestring=svg_bytes, output_width=_RASTER_WIDTH
            )
        except Exception as e:
            return ParserResult.failure(f"svg rasterize failed: {e}")
        if not png_bytes:
            return ParserResult.failure("svg rasterizer produced no output")

        try:
            with PILImage.open(io.BytesIO(png_bytes)) as img:
                width, height = img.size
        except Exception as e:
            return ParserResult.failure(f"png decode after rasterize failed: {e}")

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
                "rasterizer": "cairosvg",
                "raster_width": _RASTER_WIDTH,
            },
        )
