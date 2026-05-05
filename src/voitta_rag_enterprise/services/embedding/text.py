"""Dense text embedding (real: e5-base-v2 via sentence-transformers; fake: hash)."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from typing import TYPE_CHECKING, Any

from ..gpu_lock import gpu_lock
from .types import TextEmbedder

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

FAKE_DIM = 64


class FakeTextEmbedder(TextEmbedder):
    """Deterministic, dependency-free embedder. Same text → same vector."""

    @property
    def dim(self) -> int:
        return FAKE_DIM

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    @staticmethod
    def _vec(text: str) -> list[float]:
        # SHA-256 hash → 32 bytes → repeat / truncate to FAKE_DIM floats in [-1, 1]
        h = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [(b - 128) / 128.0 for b in h]
        out = (raw * ((FAKE_DIM // len(raw)) + 1))[:FAKE_DIM]
        # L2-normalise for cosine
        n = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / n for x in out]


class E5TextEmbedder(TextEmbedder):
    """Real e5-family encoder. Lazy-loads the model on first call."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _load_under_gpu_lock(self) -> SentenceTransformer:
        """Load the model with ``gpu_lock`` held.

        Loading transfers weights to CUDA. If another thread (e.g. a search
        request hitting embed_query while the worker is mid-extract) does
        this concurrently with someone else's CUDA work, two CUDA contexts
        race and we've seen glibc heap corruption ("malloc_consolidate:
        unaligned fastbin chunk detected"). Hold gpu_lock so model
        initialization is single-threaded against every other GPU touch.
        """
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    logger.info("loading dense text model: %s", self.model_name)
                    with gpu_lock("e5.load"):
                        self._model = SentenceTransformer(self.model_name)
        return self._model  # type: ignore[return-value]

    @property
    def dim(self) -> int:
        # ``get_embedding_dimension`` is the post-3.x name; the old
        # ``get_sentence_embedding_dimension`` still works but emits a
        # FutureWarning. We pin ST >= 3.2 in pyproject; the 3.2 line still
        # only had the old name, so fall back to it on older installs.
        model = self._load_under_gpu_lock()
        getter = getattr(
            model, "get_embedding_dimension", model.get_sentence_embedding_dimension
        )
        return int(getter())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load_under_gpu_lock()
        prefixed = [f"passage: {t}" for t in texts]
        with gpu_lock("e5.embed_documents"):
            vecs = model.encode(
                prefixed, convert_to_numpy=True, normalize_embeddings=True
            )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        model = self._load_under_gpu_lock()
        with gpu_lock("e5.embed_query"):
            vec = model.encode(
                f"query: {text}", convert_to_numpy=True, normalize_embeddings=True
            )
        return vec.tolist()
