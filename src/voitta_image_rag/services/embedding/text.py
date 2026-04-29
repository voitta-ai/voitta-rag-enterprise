"""Dense text embedding (real: e5-base-v2 via sentence-transformers; fake: hash)."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from typing import TYPE_CHECKING, Any

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

    def _ensure_loaded(self) -> SentenceTransformer:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    logger.info("loading dense text model: %s", self.model_name)
                    self._model = SentenceTransformer(self.model_name)
        return self._model  # type: ignore[return-value]

    @property
    def dim(self) -> int:
        return int(self._ensure_loaded().get_sentence_embedding_dimension())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_loaded()
        prefixed = [f"passage: {t}" for t in texts]
        vecs = model.encode(prefixed, convert_to_numpy=True, normalize_embeddings=True)
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        model = self._ensure_loaded()
        vec = model.encode(
            f"query: {text}", convert_to_numpy=True, normalize_embeddings=True
        )
        return vec.tolist()
