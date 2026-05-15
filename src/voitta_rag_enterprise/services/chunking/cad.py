"""CAD-aware chunker — splits FCStd parser output on ``## Component:``
headings so every addressable component lands in its own chunk.

The default ``ParagraphStrategy`` packs ~2000-char paragraph windows,
which means several short FCStd component sections share a chunk. That
breaks per-component retrieval: a search for "longitudinal rail L"
returns a chunk listing dozens of other parts.

This strategy keeps boundaries at the ``## Component:`` markers the
FCStd parser writes for every container / feature / orphan / whole
slug. The preamble (everything before the first marker, typically the
title + index) is one chunk; each component section is one chunk; the
trailing ``## Spreadsheets`` section (if present) is also its own chunk.

If a single component section is unusually large (> ``MAX_CHARS``), it
is split further along paragraph boundaries so embeddings never exceed
the model's token budget.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ChunkInfo, ChunkingStrategy
from .paragraph import chunk_paragraph

# Hard ceiling per chunk. Anything past this is paragraph-split. Aligned
# with ``ParagraphStrategy.DEFAULT_TARGET_CHARS * 4`` so we only fall
# back for genuinely big sections (containers listing hundreds of parts).
MAX_CHARS = 8000


_BOUNDARY_PATTERNS = (
    "\n## Component:",
    "\n## Spreadsheets",
)


def _section_starts(text: str) -> list[int]:
    """Return sorted char offsets where each chunk boundary starts.

    The first offset is always 0 (preamble). Subsequent offsets are the
    positions of each ``## Component:`` / ``## Spreadsheets`` heading —
    we keep the leading newline in the previous chunk so each section
    starts cleanly on ``##``.
    """
    starts = [0]
    for pat in _BOUNDARY_PATTERNS:
        # Walk every occurrence; ``\n`` is part of the marker so we
        # land on ``##`` directly when slicing ``text[start:]``.
        idx = 0
        while True:
            found = text.find(pat, idx)
            if found < 0:
                break
            # Skip the leading newline so the section starts on ``##``.
            starts.append(found + 1)
            idx = found + len(pat)
    return sorted(set(starts))


class CadComponentStrategy(ChunkingStrategy):
    """One chunk per addressable component for ``.fcstd``.

    Falls through to paragraph chunking for files this strategy
    doesn't claim. Register before ``ParagraphStrategy`` so its
    ``can_chunk`` short-circuits the catch-all.
    """

    def __init__(self, max_chars: int = MAX_CHARS) -> None:
        self.max_chars = max_chars

    def can_chunk(self, file_path: Path) -> bool:
        return file_path.suffix.lower() == ".fcstd"

    def chunk(self, text: str, file_path: Path) -> list[ChunkInfo]:  # noqa: ARG002
        if not text:
            return []
        starts = _section_starts(text)
        ends = starts[1:] + [len(text)]
        out: list[ChunkInfo] = []
        for s, e in zip(starts, ends):
            section = text[s:e]
            if len(section) <= self.max_chars:
                out.append(ChunkInfo(text=section, char_start=s, char_end=e))
                continue
            # Big section — paragraph-split with offsets remapped to
            # the absolute char positions in the original document so
            # downstream tooling (image anchoring, page maps) keeps
            # working.
            for sub in chunk_paragraph(section):
                out.append(ChunkInfo(
                    text=sub.text,
                    char_start=s + sub.char_start,
                    char_end=s + sub.char_end,
                ))
        # Strip empty leading chunk (file with no preamble), defensive.
        return [c for c in out if c.text.strip()]
