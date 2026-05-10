"""Pluggable chunking — paragraph default + tree-sitter cAST for code.

The indexer calls :func:`get_default_chunking_registry` and dispatches by
file path; per-strategy implementations live in sibling modules.
"""

from __future__ import annotations

from .base import ChunkInfo, ChunkingStrategy, anchor_chunk_for_position
from .code import AstCodeStrategy, chunk_code
from .languages import LANG_BY_EXT
from .paragraph import ParagraphStrategy, chunk_paragraph
from .registry import (
    ChunkingRegistry,
    build_default_registry,
    get_default_chunking_registry,
)

__all__ = [
    "AstCodeStrategy",
    "ChunkInfo",
    "ChunkingRegistry",
    "ChunkingStrategy",
    "LANG_BY_EXT",
    "ParagraphStrategy",
    "anchor_chunk_for_position",
    "build_default_registry",
    "chunk_code",
    "chunk_paragraph",
    "get_default_chunking_registry",
]
