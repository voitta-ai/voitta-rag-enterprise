"""Chunking strategy interface + ``ChunkInfo`` + image anchor helper.

Strategies are dispatched by file path. The registry walks them in order;
the first whose ``can_chunk`` returns True produces the chunks for that
file. ``ParagraphStrategy`` is the catch-all (registered last).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ChunkInfo:
    text: str
    char_start: int
    char_end: int


class ChunkingStrategy(ABC):
    """Pluggable chunker. Selection is path-based via ``can_chunk``."""

    @abstractmethod
    def can_chunk(self, file_path: Path) -> bool: ...

    @abstractmethod
    def chunk(self, text: str, file_path: Path) -> list[ChunkInfo]: ...


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
