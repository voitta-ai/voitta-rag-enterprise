"""Strategy registry — first match wins.

``ParagraphStrategy`` is registered last and matches everything, so the
registry always returns *some* answer. Code files (extensions in
``LANG_BY_EXT``) are claimed by ``AstCodeStrategy`` before they get there.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .base import ChunkInfo, ChunkingStrategy
from .code import AstCodeStrategy
from .paragraph import ParagraphStrategy


class ChunkingRegistry:
    def __init__(self) -> None:
        self._strategies: list[ChunkingStrategy] = []

    def register(self, strategy: ChunkingStrategy) -> None:
        self._strategies.append(strategy)

    def find(self, file_path: Path) -> ChunkingStrategy:
        for s in self._strategies:
            if s.can_chunk(file_path):
                return s
        raise RuntimeError(
            f"no chunking strategy matched {file_path} — "
            "ParagraphStrategy must be registered last"
        )

    def chunk(self, text: str, file_path: Path) -> list[ChunkInfo]:
        return self.find(file_path).chunk(text, file_path)

    @property
    def strategies(self) -> tuple[ChunkingStrategy, ...]:
        return tuple(self._strategies)


def build_default_registry() -> ChunkingRegistry:
    r = ChunkingRegistry()
    r.register(AstCodeStrategy())
    r.register(ParagraphStrategy())
    return r


@lru_cache(maxsize=1)
def get_default_chunking_registry() -> ChunkingRegistry:
    return build_default_registry()
