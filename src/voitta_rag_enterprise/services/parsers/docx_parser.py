"""DOCX parser via python-docx.

Walks the document body in order: paragraphs become markdown text, inline
images become ``ExtractedImage``\\ s anchored at the running char offset.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import ClassVar

from docx import Document
from docx.document import Document as _Document

from .base import BaseParser, ExtractedImage, ParserResult

logger = logging.getLogger(__name__)

_INLINE_IMG_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


class DocxParser(BaseParser):
    extensions: ClassVar[list[str]] = [".docx"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            doc: _Document = Document(str(file_path))
        except Exception as e:
            return ParserResult.failure(f"docx open failed: {e}")

        parts: list[str] = []
        images: list[ExtractedImage] = []
        cursor = 0

        for para in doc.paragraphs:
            text = para.text or ""
            paragraph_offset = cursor
            for blip in para._element.iter(_INLINE_IMG_NS):
                rid = blip.get(_R_NS)
                if not rid:
                    continue
                try:
                    image_part = doc.part.related_parts[rid]
                except KeyError:
                    continue
                blob: bytes = image_part.blob
                width, height = _dimensions(blob)
                images.append(
                    ExtractedImage(
                        bytes=blob,
                        mime=image_part.content_type,
                        position=paragraph_offset,
                        width=width,
                        height=height,
                    )
                )
            parts.append(text)
            cursor += len(text) + 2  # joiner

        content = "\n\n".join(parts)
        return ParserResult(content=content, images=images)


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return (None, None)
