"""Markdown chunker.

Char-based, paragraph-aware, with overlap. Returns ``ChunkInfo`` tuples that
record ``(text, char_start, char_end)`` so the indexer can place images at
their original char offset and find the right anchor chunk.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TARGET_CHARS = 2000  # ~512 tokens at 4 chars/token
DEFAULT_OVERLAP_CHARS = 256  # ~64 tokens
MIN_CHUNK_CHARS = 100


@dataclass
class ChunkInfo:
    text: str
    char_start: int
    char_end: int


def chunk_markdown(
    text: str,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[ChunkInfo]:
    """Split ``text`` into roughly ``target_chars`` chunks with ``overlap_chars`` overlap.

    Boundaries prefer paragraph breaks (``\\n\\n``) when one is available in the
    second half of the target window.
    """
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


def anchor_chunk_for_position(position: int, chunks: list[ChunkInfo]) -> int | None:
    """Pick the chunk index that an image at ``position`` should anchor to.

    Primary: the chunk whose ``[char_start, char_end)`` contains ``position``.
    Fallback (inter-chunk gaps): the chunk minimising
    ``min(|position - char_start|, |position - char_end|)``.
    """
    if not chunks:
        return None
    for i, c in enumerate(chunks):
        if c.char_start <= position < c.char_end:
            return i
    best_i = 0
    best_d = float("inf")
    for i, c in enumerate(chunks):
        d = min(abs(position - c.char_start), abs(position - c.char_end))
        if d < best_d:
            best_d = d
            best_i = i
    return best_i
