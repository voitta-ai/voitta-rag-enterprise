"""The real embedding models — opt-in, not run in CI.

Downloads EmbeddingGemma-2 (1.5 GB) and SigLIP-2 on first use. Run on a GPU
box or a Mac after any embedder / transformers / sentence-transformers change:

    VOITTA_REAL_EMBEDDERS=1 pytest tests/integration/test_real_embedders.py
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image as PILImage

pytestmark = pytest.mark.skipif(
    os.environ.get("VOITTA_REAL_EMBEDDERS") != "1",
    reason="real embedder run is opt-in: set VOITTA_REAL_EMBEDDERS=1",
)

CHART = Path(__file__).resolve().parents[1] / "fixtures" / "mineru4" / "full" / "images" / "page_0_chart_3.jpg"


def _cos(a: list[float], b: list[float]) -> float:
    return float(np.dot(a, b))


@pytest.fixture(scope="module")
def gemma():
    from voitta_rag_enterprise.services.embedding.gemma import GemmaEmbedder

    return GemmaEmbedder("google/embeddinggemma-2")


def test_gemma_vectors_are_768d_unit(gemma) -> None:
    for vec in (
        gemma.embed_query("quarterly revenue"),
        gemma.embed_documents(["Revenue grew in every quarter."], ["reports/annual.pdf"])[0],
        gemma.embed_image(CHART.read_bytes()),
    ):
        assert len(vec) == 768
        assert abs(np.linalg.norm(vec) - 1.0) < 1e-2


def test_gemma_images_are_not_truncated_by_the_text_cap(gemma) -> None:
    """Regression: the 512-token passage cap once applied to images too,
    and 560 vision tokens + template overflowed it (processor ValueError).
    Embedding a passage first leaves the cap set — the image must still work."""
    gemma.embed_documents(["x " * 2000])
    assert len(gemma.embed_images([CHART.read_bytes(), CHART.read_bytes()])) == 2


def test_gemma_ranks_text_and_images_sensibly(gemma) -> None:
    q = gemma.embed_query("bar chart of revenue by quarter")
    chart = gemma.embed_image(CHART.read_bytes())
    buf = io.BytesIO()
    PILImage.new("RGB", (256, 256), (200, 30, 30)).save(buf, format="PNG")
    flat = gemma.embed_image(buf.getvalue())
    assert _cos(q, chart) > _cos(q, flat)
    on, off = gemma.embed_documents(
        ["Regional revenue grew in every quarter of the fiscal year.", "The recipe needs two eggs and flour."],
        ["reports/annual.pdf", "recipes/cake.md"],
    )
    assert _cos(q, on) > _cos(q, off)
    # One space: the image-search query is the text-search query.
    assert gemma.embed_text("bar chart of revenue by quarter") == q


def test_siglip_query_padding_matters() -> None:
    """The padded query lands near its image; the old unpadded one did not."""
    import torch

    from voitta_rag_enterprise.services.embedding.image import SiglipImageEmbedder

    s = SiglipImageEmbedder("google/siglip2-base-patch16-224")
    chart = s.embed_image(CHART.read_bytes())
    padded = s.embed_text("a bar chart")
    processor, model = s._ensure_loaded()
    with torch.no_grad():
        x = processor(text=["a bar chart"], return_tensors="pt", padding=True)
        f = model.get_text_features(**x)
        f = getattr(f, "pooler_output", f)
        unpadded = torch.nn.functional.normalize(f, dim=-1)[0].tolist()
    assert _cos(padded, chart) > _cos(unpadded, chart)
