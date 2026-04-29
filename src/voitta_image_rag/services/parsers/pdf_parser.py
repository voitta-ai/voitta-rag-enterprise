"""PDF parser via PyMuPDF.

Markdown is the concatenation of per-page text, separated by ``\\n\\n``.
Images are extracted via ``page.get_images()``; each image's position is the
char offset of the start of its page's text in the produced markdown.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import ClassVar

import fitz  # PyMuPDF

from .base import BaseParser, ExtractedImage, ParserResult

logger = logging.getLogger(__name__)


class PdfParser(BaseParser):
    extensions: ClassVar[list[str]] = [".pdf"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            doc = fitz.open(file_path)
        except Exception as e:
            return ParserResult.failure(f"pdf open failed: {e}")

        parts: list[str] = []
        images: list[ExtractedImage] = []
        page_offsets: list[int] = []
        cursor = 0

        try:
            for page_index in range(len(doc)):
                page = doc[page_index]
                text = page.get_text("text") or ""
                page_offsets.append(cursor)
                parts.append(text)
                cursor += len(text) + 2  # account for the "\n\n" joiner

                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    try:
                        pix = doc.extract_image(xref)
                    except Exception as e:  # pragma: no cover — defensive
                        logger.warning("pdf image extract failed (xref=%s): %s", xref, e)
                        continue
                    img_bytes = pix.get("image")
                    if not img_bytes:
                        continue
                    ext = (pix.get("ext") or "").lower()
                    width, height = self._dimensions(img_bytes)
                    images.append(
                        ExtractedImage(
                            bytes=img_bytes,
                            mime=f"image/{ext}" if ext else "application/octet-stream",
                            position=page_offsets[page_index],
                            page=page_index + 1,
                            width=width,
                            height=height,
                        )
                    )
        finally:
            doc.close()

        content = "\n\n".join(parts)
        return ParserResult(
            content=content,
            images=images,
            metadata={"page_count": len(page_offsets)},
        )

    @staticmethod
    def _dimensions(data: bytes) -> tuple[int | None, int | None]:
        try:
            from PIL import Image as PILImage

            with PILImage.open(io.BytesIO(data)) as img:
                return img.size
        except Exception:
            return (None, None)
