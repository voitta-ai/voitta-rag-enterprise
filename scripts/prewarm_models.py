"""Pre-warm every model cache the runtime needs.

Run during ``docker build`` so the resulting image carries the weights and
the first cold start of a fresh pod has zero downloads. Reads the same
``VOITTA_*`` env vars the app does, so the cache layout matches what the
embedders look up at runtime.

Targets:

- Dense text embedder — sentence-transformers ``intfloat/e5-base-v2``
- Sparse text embedder — fastembed ``Qdrant/bm25``
- Image embedder — transformers ``google/siglip2-base-patch16-224``
- MinerU pipeline weights (layout + OCR), via the bundled CLI

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
    log.info("downloading MinerU pipeline models")
    # ``mineru-models-download`` is the supported way to pre-fetch the
    # layout + OCR weights from HuggingFace. Pipeline-only is enough for
    # the parse path we use; VLM weights are >10GB and unused here.
    subprocess.run(
        ["mineru-models-download", "-s", "huggingface", "-m", "pipeline"],
        check=True,
    )


def main() -> int:
    _dense()
    _sparse()
    _image()
    _mineru()
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
