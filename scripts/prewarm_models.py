"""Pre-warm every model cache the runtime needs.

Run during ``docker build`` so the resulting image carries the weights and
the first cold start of a fresh pod has zero downloads. Reads the same
``VOITTA_*`` env vars the app does, so the cache layout matches what the
embedders look up at runtime.

Targets:

- Dense text embedder — sentence-transformers ``intfloat/e5-base-v2``
- Sparse text embedder — fastembed ``Qdrant/bm25``
- Image embedder — transformers ``google/siglip2-base-patch16-224``
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


def _dense() -> None:
    name = os.environ.get("VOITTA_DENSE_MODEL", "intfloat/e5-base-v2")
    log.info("loading dense embedder %s", name)
    from sentence_transformers import SentenceTransformer

    SentenceTransformer(name)


def _sparse() -> None:
    name = os.environ.get("VOITTA_SPARSE_MODEL", "Qdrant/bm25")
    log.info("loading sparse embedder %s", name)
    from fastembed import SparseTextEmbedding

    SparseTextEmbedding(model_name=name)


def _image() -> None:
    name = os.environ.get("VOITTA_IMAGE_MODEL", "google/siglip2-base-patch16-224")
    log.info("loading image embedder %s", name)
    from transformers import AutoModel, AutoProcessor

    AutoProcessor.from_pretrained(name)
    AutoModel.from_pretrained(name)


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
    _dense()
    _sparse()
    _image()
    _mineru()
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
