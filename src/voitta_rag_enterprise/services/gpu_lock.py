"""Process-wide GPU mutex.

Every code path that runs inference on the GPU must enter :func:`gpu_lock`.
That includes:

* MinerU (PDF layout / OCR / formula / table models)
* the embedding models of the active profile: EmbeddingGemma-2 (text and
  images, one model) or e5-base-v2 + SigLIP-2 (real ``embed_*`` calls)

Sparse BM25 is CPU-only and does not acquire this lock.

Why a single mutex (and not per-model semaphores)? Two GPU users running at
the same time on a single device fight over VRAM and stream slots; the
result is OOMs, thrashing, or both. Until we have a real model server with
batched inference, serializing on the consumer side is the right call. The
lock also covers the query path so a search request waits behind any
in-flight indexing — accepted UX for now.

Hold time is logged at DEBUG so a tail of ``indexing.log`` shows how much
each consumer is queueing behind others.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_GPU_LOCK = threading.Lock()


@contextmanager
def gpu_lock(label: str):
    """Acquire the global GPU mutex for the duration of the block.

    ``label`` is a short tag (``"gemma.embed_images"``, ``"mineru.parse"``)
    used in the queue/hold-time log line. Keep it under ~30 chars.
    """
    queued_at = time.perf_counter()
    with _GPU_LOCK:
        wait_ms = (time.perf_counter() - queued_at) * 1000
        held_at = time.perf_counter()
        try:
            yield
        finally:
            held_ms = (time.perf_counter() - held_at) * 1000
            logger.debug(
                "gpu_lock %s wait=%.1fms held=%.1fms", label, wait_ms, held_ms
            )
