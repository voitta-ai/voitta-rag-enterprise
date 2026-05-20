"""Image+text dual embedding (real: SigLIP-2 via transformers; fake: hash)."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import threading
from typing import Any

from ..gpu_lock import gpu_lock
from .types import ImageEmbedder


class UnsupportedImageError(RuntimeError):
    """Raised when the embedder can't decode an image (WMF/EMF/corrupt).

    Caught at the indexing.py per-image boundary so the file as a whole
    still completes; one bad clipart asset shouldn't tank a 50-slide
    deck. The message includes the detected PIL format when known so
    a curious operator can grep the logs and decide whether to add a
    rasterizer.
    """

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
        """Load processor + model under ``gpu_lock``.

        Loading transfers weights to CUDA. If a search-request thread hits
        this while the indexer worker is mid-MinerU (also touching CUDA),
        two CUDA contexts race and glibc malloc detects heap corruption.
        Holding gpu_lock here keeps model initialization single-threaded
        against every other GPU consumer.
        """
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from transformers import AutoModel, AutoProcessor

                    logger.info("loading image model: %s", self.model_name)
                    with gpu_lock("siglip.load"):
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
        import numpy as np
        import torch
        from PIL import Image as PILImage

        processor, model = self._ensure_loaded()
        try:
            img = PILImage.open(io.BytesIO(data))
            img.load()  # force decode now so format-specific failures
            # surface here (WMF/EMF need an external rasterizer that
            # Pillow doesn't ship) rather than inside convert() later.
            img = img.convert("RGB")
        except Exception as e:
            # Re-raise as a clean, recognisable exception so the per-image
            # error log in indexing.py reads "unsupported image format:
            # WMF" instead of a 10-frame PIL traceback. The caller catches
            # this and continues with the next image — one unparseable
            # asset in a PowerPoint deck shouldn't sink the whole file.
            fmt = getattr(locals().get("img"), "format", None) or "unknown"
            raise UnsupportedImageError(
                f"cannot decode image (format={fmt}): {e}"
            ) from e
        # Hand the processor an explicit (H, W, 3) ndarray + channels_last so it
        # never has to guess the channel axis. SiglipImageProcessor's heuristic
        # picks the wrong axis on images where H or W equals 3 (e.g. tiny inline
        # spacers in docx), treating them as 1-channel and crashing in normalize().
        arr = np.asarray(img, dtype=np.uint8)
        inputs = processor(images=[arr], return_tensors="pt", input_data_format="channels_last")
        with gpu_lock("siglip.embed_image"), torch.no_grad():
            features = _as_tensor(model.get_image_features(**inputs))
            features = features / features.norm(dim=-1, keepdim=True)
            return features[0].cpu().tolist()

    def embed_text(self, text: str) -> list[float]:
        import torch

        processor, model = self._ensure_loaded()
        inputs = processor(text=[text], return_tensors="pt", padding=True)
        with gpu_lock("siglip.embed_text"), torch.no_grad():
            features = _as_tensor(model.get_text_features(**inputs))
            features = features / features.norm(dim=-1, keepdim=True)
            return features[0].cpu().tolist()


def _hash_to_vec(digest: bytes) -> list[float]:
    raw = [(b - 128) / 128.0 for b in digest]
    out = (raw * ((FAKE_DIM // len(raw)) + 1))[:FAKE_DIM]
    n = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / n for x in out]


def _as_tensor(result: Any) -> Any:
    """Different HF dual-encoder versions return either a bare tensor or a
    ``BaseModelOutputWithPooling`` wrapper from ``get_image_features`` /
    ``get_text_features``. Normalise to a tensor.
    """
    if hasattr(result, "norm"):
        return result
    if hasattr(result, "pooler_output") and result.pooler_output is not None:
        return result.pooler_output
    if hasattr(result, "last_hidden_state"):
        # Mean-pool patch / token embeddings as a last resort.
        return result.last_hidden_state.mean(dim=1)
    raise RuntimeError(
        f"unexpected output type from image/text encoder: {type(result).__name__}"
    )
