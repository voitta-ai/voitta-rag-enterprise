"""DOCX parser via python-docx.

Walks the document body in order: paragraphs become markdown text, inline
images become ``ExtractedImage``\\ s anchored at the running char offset. A
second pass then sweeps the OOXML package for *every other* embedded raster —
floating/anchored pictures, images in tables, and header/footer art — which the
paragraph walk doesn't reach. Those are appended at position 0 (they have no
inline char offset) so they're still embedded, searchable, and previewable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from docx import Document
from docx.document import Document as _Document

from ._ooxml import MIN_IMG_DIM, image_dimensions, iter_package_media
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
        # Zip entry names of media parts already emitted by the inline walk, so
        # the package sweep below doesn't double-count them.
        captured: set[str] = set()

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
                width, height = image_dimensions(blob)
                images.append(
                    ExtractedImage(
                        bytes=blob,
                        mime=image_part.content_type,
                        position=paragraph_offset,
                        width=width,
                        height=height,
                    )
                )
                # partname is a PackURI like "/word/media/image1.png"; the zip
                # entry has no leading slash.
                captured.add(str(image_part.partname).lstrip("/"))
            parts.append(text)
            cursor += len(text) + 2  # joiner

        content = "\n\n".join(parts)

        # Sweep the package for everything the inline walk can't reach:
        # anchored/floating pictures, images inside tables, and header/footer
        # art. Skipped if already captured inline; tiny decorative glyphs are
        # filtered out.
        for _name, blob, mime in iter_package_media(
            file_path, media_prefix="word/media/", skip_names=captured
        ):
            width, height = image_dimensions(blob)
            if width and height and max(width, height) < MIN_IMG_DIM:
                continue
            images.append(
                ExtractedImage(
                    bytes=blob, mime=mime, position=0, width=width, height=height
                )
            )

        return ParserResult(content=content, images=images)
