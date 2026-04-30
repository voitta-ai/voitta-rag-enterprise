"""Plain-text / source-code parser. No images."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from .base import BaseParser, ParserResult


class TextParser(BaseParser):
    extensions: ClassVar[list[str]] = [
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".log",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".sql",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
    ]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParserResult.failure(f"text read failed: {e}")
        return ParserResult(content=content)
