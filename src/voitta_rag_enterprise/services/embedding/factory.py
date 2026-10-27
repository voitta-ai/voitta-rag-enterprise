"""Cached factories returning the active profile's embedders.

Under a unified profile (``gemma2``) ``get_text_embedder()`` and
``get_image_embedder()`` return the SAME instance: one model, loaded once,
one vector space for text and images.
"""

from __future__ import annotations

import logging

from ...config import get_settings
from .gemma import FakeGemmaEmbedder, GemmaEmbedder
from .image import FakeImageEmbedder, SiglipImageEmbedder
from .profiles import active, reset_profile_cache
from .sparse import Bm25SparseEmbedder, FakeSparseEmbedder
from .text import E5TextEmbedder, FakeTextEmbedder
from .types import ImageEmbedder, SparseEmbedder, TextEmbedder

logger = logging.getLogger(__name__)

_text: TextEmbedder | None = None
_sparse: SparseEmbedder | None = None
_image: ImageEmbedder | None = None


def get_text_embedder() -> TextEmbedder:
    global _text
    if _text is None:
        _text = _build_text_embedder()
    return _text


def get_sparse_embedder() -> SparseEmbedder:
    global _sparse
    if _sparse is None:
        _sparse = _build_sparse_embedder()
    return _sparse


def get_image_embedder() -> ImageEmbedder:
    global _image
    if _image is None:
        _image = _build_image_embedder()
    return _image


def reset_embedder_caches() -> None:
    """Drop cached embedders and the resolved profile (tests, profile switch)."""
    global _text, _sparse, _image, _shared
    _text = _sparse = _image = _shared = None
    reset_profile_cache()


def _build_text_embedder() -> TextEmbedder:
    prof = active()
    if prof.unified:
        return _unified()
    if get_settings().use_fake_embedders:
        return FakeTextEmbedder()
    try:
        return E5TextEmbedder(prof.dense_model)
    except ImportError as e:
        logger.warning("dense embedder unavailable (%s) — using fake", e)
        return FakeTextEmbedder()


def _build_sparse_embedder() -> SparseEmbedder:
    settings = get_settings()
    if settings.use_fake_embedders:
        return FakeSparseEmbedder()
    try:
        return Bm25SparseEmbedder(settings.sparse_model)
    except ImportError as e:
        logger.warning("sparse embedder unavailable (%s) — using fake", e)
        return FakeSparseEmbedder()


def _build_image_embedder() -> ImageEmbedder:
    prof = active()
    if prof.unified:
        return _unified()
    if get_settings().use_fake_embedders:
        return FakeImageEmbedder()
    try:
        return SiglipImageEmbedder(prof.image_model)
    except ImportError as e:
        logger.warning("image embedder unavailable (%s) — using fake", e)
        return FakeImageEmbedder()


_shared: GemmaEmbedder | FakeGemmaEmbedder | None = None


def _unified() -> GemmaEmbedder | FakeGemmaEmbedder:
    """The one text+image embedder of a unified profile."""
    global _shared
    if _shared is None:
        if get_settings().use_fake_embedders:
            _shared = FakeGemmaEmbedder()
        else:
            _shared = GemmaEmbedder(active().dense_model)
    return _shared
