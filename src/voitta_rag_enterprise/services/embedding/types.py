"""Embedder protocols. Real and fake implementations conform to these."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]


class TextEmbedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SparseEmbedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[SparseVector]: ...

    def embed_query(self, text: str) -> SparseVector: ...


class ImageEmbedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed_image(self, data: bytes) -> list[float]: ...

    def embed_text(self, text: str) -> list[float]: ...
