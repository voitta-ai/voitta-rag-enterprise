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
class ParserResult:
    content: str
    images: list[ExtractedImage] = field(default_factory=list)
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
