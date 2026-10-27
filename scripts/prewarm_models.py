"""Pre-warm every model cache the runtime needs.

Run during ``docker build`` so the resulting image carries the weights and
the first cold start of a fresh pod has zero downloads. Reads the same
``VOITTA_*`` env vars the app does, so the cache layout matches what the
embedders look up at runtime.

Targets:

- The dense + image models of each profile in ``PREWARM_PROFILES``
  (default ``gemma2``: EmbeddingGemma-2 for both; ``e5-siglip``: e5-base-v2 +
  SigLIP-2)
- Sparse text embedder — fastembed ``Qdrant/bm25``
- MinerU ``basic``-tier weights (layout, OCR, formula, table; Torch and
  ONNX backends) into ``$MINERU_HOME/models``, via MinerU's own CLI

Each step is wrapped so a single network blip can be retried without
re-running the whole build.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="prewarm: %(message)s")
log = logging.getLogger(__name__)


def _profile_models(name: str) -> None:
    """Download one embedding profile's dense + image models."""
    from voitta_rag_enterprise.services.embedding.profiles import profile

    p = profile(name)
    if p.unified:
        # EmbeddingGemma-2 loads through sentence-transformers; the audio
        # tower is skipped at runtime, but its weights ship in the same file.
        log.info("loading %s model %s", name, p.dense_model)
        from sentence_transformers import SentenceTransformer

        SentenceTransformer(p.dense_model, config_kwargs={"audio_config": None})
        return
    log.info("loading %s dense model %s", name, p.dense_model)
    from sentence_transformers import SentenceTransformer

    SentenceTransformer(p.dense_model)
    log.info("loading %s image model %s", name, p.image_model)
    from transformers import AutoModel, AutoProcessor

    AutoProcessor.from_pretrained(p.image_model)
    AutoModel.from_pretrained(p.image_model)


def _sparse() -> None:
    name = os.environ.get("VOITTA_SPARSE_MODEL", "Qdrant/bm25")
    log.info("loading sparse embedder %s", name)
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=name)


def _mineru() -> None:
    source = os.environ.get("MINERU_MODEL_SOURCE", "huggingface")
    log.info("downloading MinerU basic-tier models from %s", source)
    # ``python -m mineru.kit.main`` rather than the ``mineru-kit`` console
    # script, so this works from any interpreter that has mineru installed.
    # Basic tier only: the VLM tiers' weights are many GB and unused here.
    # Both small-model backends: MinerU picks torch when it sees a GPU and
    # ONNX on CPU, and the build host cannot know which the runtime has.
    kit = [sys.executable, "-m", "mineru.kit.main", "models"]
    for backend in ("torch", "onnx"):
        tier = ["--tier", "basic", "--small-backend", backend]
        subprocess.run([*kit, "download", *tier, "--source", source], check=True)
        subprocess.run([*kit, "verify", *tier], check=True)


def main() -> int:
    # PREWARM_PROFILES (comma-separated) picks which embedding profiles'
    # weights to bake in. New installs need only gemma2; an image for an
    # existing e5-siglip deployment that will migrate needs both.
    for name in os.environ.get("PREWARM_PROFILES", "gemma2").split(","):
        _profile_models(name.strip())
    _sparse()
    _mineru()
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
