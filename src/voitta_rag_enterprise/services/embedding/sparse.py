"""Sparse text embedding (real: Qdrant BM25 via fastembed; fake: word-hash)."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Any

from .types import SparseEmbedder, SparseVector

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+")


class FakeSparseEmbedder(SparseEmbedder):
    """Word-hash sparse vectors. Tokens are hashed into a fixed-size vocab."""

    VOCAB = 4096

    def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> SparseVector:
        return self._vec(text)

    def _vec(self, text: str) -> SparseVector:
        counts: dict[int, int] = {}
        for tok in _TOKEN_RE.findall(text.lower()):
            idx = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little") % self.VOCAB
            counts[idx] = counts.get(idx, 0) + 1
        if not counts:
            return SparseVector(indices=[], values=[])
        items = sorted(counts.items())
        return SparseVector(
            indices=[i for i, _ in items],
            values=[float(v) for _, v in items],
        )


class Bm25SparseEmbedder(SparseEmbedder):
    """Real BM25 via Qdrant fastembed. Lazy-loaded."""

    def __init__(self, model_name: str = "Qdrant/bm25") -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import SparseTextEmbedding

                    logger.info("loading sparse model: %s", self.model_name)
                    self._model = SparseTextEmbedding(model_name=self.model_name)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        model = self._ensure_loaded()
        out: list[SparseVector] = []
        for r in model.embed(texts):
            out.append(SparseVector(indices=r.indices.tolist(), values=r.values.tolist()))
        return out

    def embed_query(self, text: str) -> SparseVector:
        model = self._ensure_loaded()
        r = next(iter(model.query_embed([text])))
        return SparseVector(indices=r.indices.tolist(), values=r.values.tolist())
