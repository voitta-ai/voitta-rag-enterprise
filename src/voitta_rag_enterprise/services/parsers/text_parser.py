"""Plain-text / source-code parser. No images."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from .base import BaseParser, ParserResult


class TextParser(BaseParser):
    # Anything that's plain UTF-8 text. The dispatcher (registry.py) hands a
    # file to this parser iff its extension is in this list, so missing
    # languages here = silently dropped on indexing. Better default would be
    # content-sniffing; until then the list errs on the side of inclusive.
    extensions: ClassVar[list[str]] = [
        # plain text + docs
        ".txt", ".md", ".markdown", ".rst", ".log", ".tex", ".bib",
        ".adoc", ".asciidoc", ".org", ".patch", ".diff",
        # data + config
        ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".csv", ".tsv",
        ".xml", ".ini", ".cfg", ".conf", ".env", ".properties", ".sql",
        # web
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".styl",
        ".vue", ".svelte", ".astro", ".php",
        # python / js / ts
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
        # systems
        ".c", ".cc", ".cxx", ".cpp", ".h", ".hh", ".hxx", ".hpp",
        ".go", ".rs", ".zig", ".nim",
        # JVM
        ".java", ".kt", ".kts", ".scala", ".sc", ".groovy",
        ".clj", ".cljs", ".cljc",
        # .NET
        ".cs", ".vb", ".fs", ".fsx", ".fsi",
        # apple
        ".swift", ".m", ".mm",
        # other major
        ".rb", ".lua", ".pl", ".pm", ".r", ".dart", ".jl",
        ".ex", ".exs", ".erl", ".hs", ".lhs", ".ml", ".mli",
        ".pas", ".f", ".f90", ".f95", ".cr",
        # shell + build
        ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh",
        ".gradle", ".cmake", ".mk",
    ]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParserResult.failure(f"text read failed: {e}")
        return ParserResult(content=content)
