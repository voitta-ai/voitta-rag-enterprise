"""Parser registry — dispatches a file to the first matching parser."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .base import BaseParser


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: list[BaseParser] = []

    def register(self, parser: BaseParser) -> None:
        self._parsers.append(parser)

    def find(self, file_path: Path) -> BaseParser | None:
        for p in self._parsers:
            if p.can_parse(file_path):
                return p
        return None

    @property
    def parsers(self) -> tuple[BaseParser, ...]:
        return tuple(self._parsers)


def build_default_registry() -> ParserRegistry:
    # Local imports keep heavy parser dependencies out of the import path
    # for callers that only want, say, the text parser.
    from .cad_step_parser import CadStepParser
    from .docx_parser import DocxParser
    from .image_parser import ImageFileParser
    from .ipynb_parser import IpynbParser
    from .pdf_parser import PdfParser
    from .pptx_parser import PptxParser
    from .svg_parser import SvgParser
    from .text_parser import TextParser
    from .xlsx_parser import XlsxParser

    r = ParserRegistry()
    # IpynbParser before TextParser so .ipynb is claimed by the structured
    # parser even though TextParser doesn't currently include it — keeps
    # the precedence intentional in case the text list grows.
    r.register(IpynbParser())
    r.register(TextParser())
    r.register(PdfParser())
    r.register(DocxParser())
    r.register(PptxParser())
    r.register(XlsxParser())
    r.register(SvgParser())
    r.register(ImageFileParser())
    r.register(CadStepParser())
    return r


@lru_cache(maxsize=1)
def get_default_registry() -> ParserRegistry:
    return build_default_registry()
