"""Parser base types: ``Parser`` ABC + ``ParseResult`` / ``ExtractedImage``.

Concrete parsers in this package are dispatched by file extension via
``registry.get_default_registry()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass
class ExtractedImage:
    bytes: bytes
    mime: str
    position: int  # char offset into the produced markdown
    page: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class RenderedPage:
    """A full-page raster of a source-document page, surfaced to the LLM
    purely as layout context. Distinct from ``ExtractedImage`` (cropped
    figures): these are not embedded for image search and have no anchor
    chunk — they are fetched on demand by ``(file_id, page)``."""

    bytes: bytes
    mime: str  # always "image/webp" today
    page: int  # 1-indexed
    width: int | None = None
    height: int | None = None


@dataclass
class ParserResult:
    content: str
    images: list[ExtractedImage] = field(default_factory=list)
    page_images: list[RenderedPage] = field(default_factory=list)
    # Per-block layout list passed through from the parser unchanged
    # (today only the PDF parser populates this, from MinerU's
    # ``*_content_list.json``). Each block carries at least ``type`` and
    # ``page`` (1-indexed, absolute across buckets); additional fields
    # like ``bbox``, ``text``, ``img_path`` are passed through as-is so
    # downstream consumers can lean on whatever MinerU emits without us
    # re-modeling it. Empty for parsers that don't have layout.
    page_layout: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    success: bool = True
    error: str | None = None

    @classmethod
    def failure(cls, error: str) -> ParserResult:
        return cls(content="", images=[], metadata={}, success=False, error=error)


class BaseParser(ABC):
    extensions: ClassVar[list[str]] = []

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.extensions

    @abstractmethod
    def parse(self, file_path: Path) -> ParserResult: ...
