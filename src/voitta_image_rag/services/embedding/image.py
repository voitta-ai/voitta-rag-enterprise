"""Image+text dual embedding (real: SigLIP-2 via transformers; fake: hash)."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import threading
from typing import Any

from .types import ImageEmbedder

logger = logging.getLogger(__name__)

FAKE_DIM = 64


class FakeImageEmbedder(ImageEmbedder):
    """Deterministic image+text embedder. Hash-based, same input → same vector."""

    @property
    def dim(self) -> int:
        return FAKE_DIM

    def embed_image(self, data: bytes) -> list[float]:
        return _hash_to_vec(hashlib.sha256(data).digest())

    def embed_text(self, text: str) -> list[float]:
        return _hash_to_vec(hashlib.sha256(text.encode("utf-8")).digest())


class SiglipImageEmbedder(ImageEmbedder):
    """Real SigLIP-2 (or any HF dual-encoder). Lazy-loaded."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._processor: Any | None = None
        self._dim: int | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> tuple[Any, Any]:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from transformers import AutoModel, AutoProcessor

                    logger.info("loading image model: %s", self.model_name)
                    self._processor = AutoProcessor.from_pretrained(self.model_name)
                    model = AutoModel.from_pretrained(self.model_name)
                    model.eval()
                    self._model = model
        return self._processor, self._model  # type: ignore[return-value]

    @property
    def dim(self) -> int:
        if self._dim is not None:
            return self._dim
        # CLIP/SigLIP/BLIP all expose hidden_size on the text/vision sub-config.
        # Fall back to embedding a tiny string and measuring if the attribute
        # path differs.
        _, model = self._ensure_loaded()
        cfg = model.config
        for attr in ("projection_dim",):
            if hasattr(cfg, attr):
                self._dim = int(getattr(cfg, attr))
                return self._dim
        for sub_attr in ("text_config", "vision_config"):
            sub = getattr(cfg, sub_attr, None)
            if sub is not None and hasattr(sub, "hidden_size"):
                self._dim = int(sub.hidden_size)
                return self._dim
        # Last resort: probe.
        self._dim = len(self.embed_text("dim probe"))
        return self._dim

    def embed_image(self, data: bytes) -> list[float]:
        import torch
        from PIL import Image as PILImage

        processor, model = self._ensure_loaded()
        img = PILImage.open(io.BytesIO(data)).convert("RGB")
        inputs = processor(images=[img], return_tensors="pt")
        with torch.no_grad():
            features = model.get_image_features(**inputs)
        features = features / features.norm(dim=-1, keepdim=True)
        return features[0].cpu().tolist()

    def embed_text(self, text: str) -> list[float]:
        import torch

        processor, model = self._ensure_loaded()
        inputs = processor(text=[text], return_tensors="pt", padding=True)
        with torch.no_grad():
            features = model.get_text_features(**inputs)
        features = features / features.norm(dim=-1, keepdim=True)
        return features[0].cpu().tolist()


def _hash_to_vec(digest: bytes) -> list[float]:
    raw = [(b - 128) / 128.0 for b in digest]
    out = (raw * ((FAKE_DIM // len(raw)) + 1))[:FAKE_DIM]
    n = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / n for x in out]
