"""Paragraph-aware char chunker — the default for prose / markdown / data.

Boundaries prefer ``\\n\\n`` in the second half of each window. Consecutive
chunks overlap by ``overlap_chars`` so a paragraph straddling a window
boundary still appears in full inside one of the two chunks.

Registered last in ``ChunkingRegistry`` — its ``can_chunk`` is a catch-all.
"""

from __future__ import annotations

from pathlib import Path

from .base import ChunkInfo, ChunkingStrategy

DEFAULT_TARGET_CHARS = 2000  # ~512 tokens at 4 chars/token
DEFAULT_OVERLAP_CHARS = 256  # ~64 tokens


def chunk_paragraph(
    text: str,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[ChunkInfo]:
    """Split ``text`` into roughly ``target_chars`` chunks with overlap."""
    n = len(text)
    if n == 0:
        return []
    if n <= target_chars:
        return [ChunkInfo(text=text, char_start=0, char_end=n)]

    chunks: list[ChunkInfo] = []
    pos = 0
    while pos < n:
        end = min(pos + target_chars, n)
        if end < n:
            search_from = pos + target_chars // 2
            paragraph_break = text.rfind("\n\n", search_from, end)
            if paragraph_break != -1:
                end = paragraph_break + 2
        chunks.append(ChunkInfo(text=text[pos:end], char_start=pos, char_end=end))
        if end >= n:
            break
        next_pos = end - overlap_chars
        pos = next_pos if next_pos > pos else end
    return chunks


class ParagraphStrategy(ChunkingStrategy):
    """Catch-all strategy for non-code files.

    ``can_chunk`` always returns True so this must be the last strategy
    registered.
    """

    def __init__(
        self,
        target_chars: int = DEFAULT_TARGET_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> None:
        self.target_chars = target_chars
        self.overlap_chars = overlap_chars

    def can_chunk(self, file_path: Path) -> bool:  # noqa: ARG002
        return True

    def chunk(self, text: str, file_path: Path) -> list[ChunkInfo]:  # noqa: ARG002
        return chunk_paragraph(text, self.target_chars, self.overlap_chars)
