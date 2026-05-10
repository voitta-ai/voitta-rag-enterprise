"""Source-code chunker — cAST-style splits via ``tree-sitter-language-pack``.

The library's :func:`tree_sitter_language_pack.process` does the partition
+ merge pass natively in Rust: descend the AST, emit nodes that fit
within ``chunk_max_size`` bytes, descend further when they don't, and
greedily pack adjacent siblings up to the budget. We just translate the
``CodeChunk`` records it returns into our ``ChunkInfo`` shape (char
offsets instead of byte offsets) and stretch the first/last chunk to
cover any leading/trailing whitespace so the chunk list spans the whole
file (required by ``anchor_chunk_for_position``).

``chunk_max_size`` is bytes; ``ChunkInfo`` exposes character offsets, so
we build a byte→char map up front.
"""

from __future__ import annotations

from pathlib import Path

from .base import ChunkInfo, ChunkingStrategy
from .languages import LANG_BY_EXT

# Default budget. ~2000 bytes ≈ ~500 e5-base-v2 tokens (well under its 512
# cap). Mirrors ``ParagraphStrategy``'s target so embedding behavior is
# uniform across strategies.
DEFAULT_TARGET_BYTES = 2000


def _byte_to_char_map(text: str) -> list[int]:
    """``mapping[byte_idx]`` → corresponding char index. Length is
    ``len(utf8(text)) + 1`` so the end-byte index is always valid."""
    mapping: list[int] = []
    char_idx = 0
    for ch in text:
        b_len = len(ch.encode("utf-8"))
        mapping.extend([char_idx] * b_len)
        char_idx += 1
    mapping.append(char_idx)
    return mapping


def _stretch_to_full_coverage(
    chunks: list[ChunkInfo], text: str, target_chars: int
) -> list[ChunkInfo]:
    """Extend the first chunk back to char 0 and the last chunk forward to
    ``len(text)`` so consumers can rely on full coverage. If extending
    would push a chunk past ``target_chars`` we emit the leading / trailing
    span as its own chunk instead.
    """
    if not chunks:
        return chunks
    n = len(text)

    first = chunks[0]
    if first.char_start > 0:
        if first.char_end <= target_chars:
            chunks[0] = ChunkInfo(
                text=text[0 : first.char_end],
                char_start=0,
                char_end=first.char_end,
            )
        else:
            chunks.insert(
                0,
                ChunkInfo(
                    text=text[0 : first.char_start],
                    char_start=0,
                    char_end=first.char_start,
                ),
            )

    last = chunks[-1]
    if last.char_end < n:
        if n - last.char_start <= target_chars:
            chunks[-1] = ChunkInfo(
                text=text[last.char_start : n],
                char_start=last.char_start,
                char_end=n,
            )
        else:
            chunks.append(
                ChunkInfo(
                    text=text[last.char_end : n],
                    char_start=last.char_end,
                    char_end=n,
                )
            )
    return chunks


def chunk_code(
    text: str, lang_name: str, target_chars: int = DEFAULT_TARGET_BYTES
) -> list[ChunkInfo]:
    """Public entry point: tree-sitter cAST chunking for one language."""
    if not text:
        return []

    from tree_sitter_language_pack import ProcessConfig, process

    cfg = ProcessConfig(
        language=lang_name,
        chunk_max_size=target_chars,
        # We only want the chunks; turn the metadata extractors off so we
        # don't pay for symbol tables / docstrings / diagnostics on every
        # indexed file.
        structure=False,
        imports=False,
        exports=False,
        comments=False,
        docstrings=False,
        symbols=False,
        diagnostics=False,
    )
    result = process(text, cfg)

    if not result.chunks:
        # Whitespace-only / unparseable file. Either fits in one chunk or
        # we emit a single span covering the whole text — no fallback to
        # paragraph chunking, just one big chunk.
        return [ChunkInfo(text=text, char_start=0, char_end=len(text))]

    b2c = _byte_to_char_map(text)
    chunks: list[ChunkInfo] = []
    for c in result.chunks:
        cs = b2c[c.start_byte]
        ce = b2c[c.end_byte]
        if ce <= cs:
            continue
        chunks.append(ChunkInfo(text=text[cs:ce], char_start=cs, char_end=ce))

    if not chunks:
        return [ChunkInfo(text=text, char_start=0, char_end=len(text))]

    return _stretch_to_full_coverage(chunks, text, target_chars)


class AstCodeStrategy(ChunkingStrategy):
    """Tree-sitter chunker for source code.

    ``can_chunk`` returns True iff the file extension is in
    :data:`~voitta_rag_enterprise.services.chunking.languages.LANG_BY_EXT`.
    """

    def __init__(self, target_chars: int = DEFAULT_TARGET_BYTES) -> None:
        self.target_chars = target_chars

    def can_chunk(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in LANG_BY_EXT

    def chunk(self, text: str, file_path: Path) -> list[ChunkInfo]:
        lang = LANG_BY_EXT[file_path.suffix.lower()]
        return chunk_code(text, lang, self.target_chars)
