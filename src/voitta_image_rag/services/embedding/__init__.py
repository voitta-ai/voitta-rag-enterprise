"""Embedding backends — text dense, text sparse, image."""

from .factory import (
    get_image_embedder,
    get_sparse_embedder,
    get_text_embedder,
    reset_embedder_caches,
)
from .types import ImageEmbedder, SparseEmbedder, SparseVector, TextEmbedder

__all__ = [
    "ImageEmbedder",
    "SparseEmbedder",
    "SparseVector",
    "TextEmbedder",
    "get_image_embedder",
    "get_sparse_embedder",
    "get_text_embedder",
    "reset_embedder_caches",
]
