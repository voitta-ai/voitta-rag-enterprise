"""EmbeddingGemma-2: one model for text passages, search queries and images.

Implements both ``TextEmbedder`` and ``ImageEmbedder`` — the factory hands the
same instance out for both under the ``gemma2`` profile, so the weights load
once and text and images share one 768-d space. Text-to-image search embeds
the query exactly like text search does (``SearchQuery`` prompt); images go
in as raw pixels with no prompt, per the model card.

Encoding matches what the 2026-10 evaluation measured
(docs/plans/gemma-migration.md): ``title: {rel_path} | text: …`` passages
capped at 512 tokens (the file path in the title slot beat ``title: none`` by
+3.0 MRR@10 [+0.6, +5.3] on our folders), ``task: search result | query: …``
queries, 560 vision tokens per image, bf16 on CUDA. The audio tower is never
loaded.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from typing import Any

from ..gpu_lock import gpu_lock
from .image import UnsupportedImageError, _hash_to_vec
from .types import ImageEmbedder, TextEmbedder

logger = logging.getLogger(__name__)

# Token cap for passages: our chunker targets ~512 tokens and the evaluation
# ran at this length. Longer input is truncated, not rejected.
MAX_SEQ_TOKENS = 512
# Vision tokens per image. Measured on 20,000 of our figures / 654 queries:
# 560 tokens 82.3 MRR@10 vs 76.5 at the model's default 280 (+5.8, 95 % CI
# [+3.8, +8.0]) at 2.4x the cost (8 vs 20 img/s on an RTX 5070).
VISION_TOKENS = 560
# Images per forward pass: 8 at 560 tokens peaked at 2.3 GB.
IMAGE_BATCH = 8
_QUERY_PROMPT = "SearchQuery"
_TEXT_BATCH = 32


class GemmaEmbedder(TextEmbedder, ImageEmbedder):
    """Real EmbeddingGemma-2 via sentence-transformers. Lazy-loaded."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._unlimited: int | None = None  # the model's own max_seq_length
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> Any:
        """Load under ``gpu_lock`` — see ``E5TextEmbedder._load_under_gpu_lock``
        for why a model load must never overlap another thread's CUDA work."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    import torch
                    from sentence_transformers import SentenceTransformer

                    cuda = torch.cuda.is_available()
                    logger.info("loading embedding model: %s", self.model_name)
                    with gpu_lock("gemma.load"):
                        model = SentenceTransformer(
                            self.model_name,
                            model_kwargs={"dtype": torch.bfloat16 if cuda else torch.float32},
                            config_kwargs={"audio_config": None},
                        )
                        self._unlimited = model.max_seq_length
                        processor = model[0].processor
                        processor.image_processor.max_soft_tokens = VISION_TOKENS
                        # The processor expands the image placeholder to this
                        # many tokens; it must agree with the image processor.
                        if hasattr(processor, "image_seq_length"):
                            processor.image_seq_length = VISION_TOKENS
                    self._model = model
        return self._model

    @property
    def dim(self) -> int:
        return int(self._ensure_loaded().get_embedding_dimension())

    @staticmethod
    def _encode(model: Any, inputs: Any, max_len: int | None, **kw: Any) -> Any:
        """``model.encode`` with a per-call token cap. sentence-transformers
        keeps the cap on the model; every caller holds ``gpu_lock``, so no
        other encode can observe the value set here."""
        model.max_seq_length = max_len
        return model.encode(inputs, convert_to_numpy=True, normalize_embeddings=True, **kw)

    # -- text ---------------------------------------------------------------

    def embed_documents(
        self, texts: list[str], titles: list[str] | None = None
    ) -> list[list[float]]:
        model = self._ensure_loaded()
        titles = titles or [""] * len(texts)
        docs = [
            f"title: {title or 'none'} | text: {t}"
            for t, title in zip(texts, titles, strict=True)
        ]
        with gpu_lock("gemma.embed_documents"):
            vecs = self._encode(model, docs, MAX_SEQ_TOKENS, batch_size=_TEXT_BATCH)
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        model = self._ensure_loaded()
        with gpu_lock("gemma.embed_query"):
            vec = self._encode(model, text, MAX_SEQ_TOKENS, prompt_name=_QUERY_PROMPT)
        return vec.tolist()

    # -- images -------------------------------------------------------------

    def embed_text(self, text: str) -> list[float]:
        """Text side of text-to-image search: the same vector as a text query."""
        return self.embed_query(text)

    def embed_image(self, data: bytes) -> list[float]:
        return self.embed_images([data])[0]

    def embed_images(self, blobs: list[bytes]) -> list[list[float]]:
        model = self._ensure_loaded()
        images = [_decode(b) for b in blobs]
        out: list[list[float]] = []
        for start in range(0, len(images), IMAGE_BATCH):
            batch = images[start : start + IMAGE_BATCH]
            with gpu_lock("gemma.embed_images"):
                # No token cap: an image alone is VISION_TOKENS plus the
                # chat template, and truncating it breaks the processor.
                vecs = self._encode(model, batch, self._unlimited, batch_size=len(batch))
            out.extend(v.tolist() for v in vecs)
        return out


def _decode(data: bytes):
    """Bytes → RGB PIL image, or ``UnsupportedImageError`` (same contract as SigLIP)."""
    from PIL import Image as PILImage

    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception as e:
        fmt = getattr(locals().get("img"), "format", None) or "unknown"
        raise UnsupportedImageError(f"cannot decode image (format={fmt}): {e}") from e


class FakeGemmaEmbedder(TextEmbedder, ImageEmbedder):
    """Deterministic stand-in for tests: one hash space for text, queries and
    images, so a query and an image can only match by identical input."""

    @property
    def dim(self) -> int:
        from .image import FAKE_DIM

        return FAKE_DIM

    def embed_documents(
        self, texts: list[str], titles: list[str] | None = None
    ) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return _hash_to_vec(hashlib.sha256(text.encode("utf-8")).digest())

    def embed_text(self, text: str) -> list[float]:
        return self.embed_query(text)

    def embed_image(self, data: bytes) -> list[float]:
        return _hash_to_vec(hashlib.sha256(data).digest())
