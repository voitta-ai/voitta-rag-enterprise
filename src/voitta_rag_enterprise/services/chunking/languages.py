"""File-extension → tree-sitter grammar name.

Only languages we have verified grammars for via ``tree-sitter-language-pack``.
Anything not in this map falls through to ``ParagraphStrategy`` via the
registry, so adding a new language is one entry here plus (optionally) a
test in ``tests/unit/chunking/test_code.py``.
"""

from __future__ import annotations

LANG_BY_EXT: dict[str, str] = {
    # python
    ".py": "python",
    ".pyi": "python",
    # js / ts
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    # systems
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    # JVM
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    # .NET
    ".cs": "csharp",
    # other major
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".lua": "lua",
    # shell
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    # web (treated as code: HTML/CSS have block structure tree-sitter can split on)
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    # data-ish languages with real grammar
    ".sql": "sql",
}
