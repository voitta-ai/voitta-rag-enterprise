"""Parser base types: ``Parser`` ABC + ``ParseResult`` / ``ExtractedImage``.

Concrete parsers in this package are dispatched by file extension via
``registry.get_default_registry()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..asset_handlers import AssetSpec


class UnsupportedDocumentError(Exception):
    """Raised by a parser when a file is well-formed but cannot be indexed
    for a reason that is not an indexer failure — e.g. a password-protected
    PDF we have no password for. The extractor parks these in the
    ``unsupported`` state (with ``reason`` as the stored message) rather than
    ``error``, so they don't pollute the error counters. Reindex will retry.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    # Sparse char-offset → page map. Each entry ``(char_offset, page)`` says
    # "from this char in ``content`` onward, blocks belong to ``page`` until
    # the next entry overrides it." Lookup is a binary search to the
    # largest offset ≤ query, returning that entry's page. Chunkers don't
    # need to think about this; the indexer pulls a chunk's page out of
    # the merged map at embed time. Empty for parsers that don't emit it.
    char_to_page: list[tuple[int, int]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # On-demand assets the parser surfaces for this file — the LLM
    # invokes them through the ``request_asset`` MCP tool. The parser
    # describes the menu (what's available, which slugs, what params);
    # the actual rendering / computation happens later via a handler
    # registered in ``services.asset_handlers``. Parsers populate this
    # at extract time so ``list_assets(file_id)`` knows the answer
    # without re-parsing. Empty for parsers with nothing to expose.
    on_demand_assets: list["AssetSpec"] = field(default_factory=list)
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
