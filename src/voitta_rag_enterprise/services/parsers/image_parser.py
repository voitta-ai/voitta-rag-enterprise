"""Standalone-image parser. Empty markdown + one ExtractedImage."""

from __future__ import annotations

import io
from pathlib import Path
from typing import ClassVar

from PIL import Image as PILImage

from .base import BaseParser, ExtractedImage, ParserResult


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
        try:
            with PILImage.open(io.BytesIO(data)) as img:
                width, height = img.size
                fmt = (img.format or "").lower()
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
