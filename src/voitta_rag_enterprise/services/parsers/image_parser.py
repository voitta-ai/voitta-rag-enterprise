"""Standalone-image parser. Empty markdown + one ExtractedImage."""

from __future__ import annotations

import io
from pathlib import Path
from typing import ClassVar

from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from .base import (
    BaseParser,
    ExtractedImage,
    ParserResult,
    UnsupportedDocumentError,
)


class ImageFileParser(BaseParser):
    extensions: ClassVar[list[str]] = [
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    ]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            data = file_path.read_bytes()
        except OSError as e:
            return ParserResult.failure(f"image read failed: {e}")
        # An empty file with an image extension is not an indexer failure — it's
        # a placeholder (e.g. OrcaSlicer commits a 0-byte extra_icon.png). Park
        # it as unsupported instead of erroring. Same for bytes PIL can't even
        # identify as an image format: they fail identically every run, so
        # counting them as errors just adds noise. Genuine mid-decode failures
        # of a real format (truncation, decompression bomb) still surface as
        # errors below.
        if not data:
            raise UnsupportedDocumentError("empty image file (0 bytes)")
        try:
            with PILImage.open(io.BytesIO(data)) as img:
                width, height = img.size
                fmt = (img.format or "").lower()
        except UnidentifiedImageError as e:
            raise UnsupportedDocumentError(f"not a decodable image: {e}") from e
        except Exception as e:
            return ParserResult.failure(f"image decode failed: {e}")
        return ParserResult(
            content="",
            images=[
                ExtractedImage(
                    bytes=data,
                    mime=f"image/{fmt}" if fmt else "application/octet-stream",
                    position=0,
                    page=None,
                    width=width,
                    height=height,
                )
            ],
        )
