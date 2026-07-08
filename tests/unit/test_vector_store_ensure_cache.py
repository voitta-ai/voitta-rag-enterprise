"""Phase-1 indexing-throughput guards.

1. ``ensure_*_collection`` is cached per process: the existence check +
   ~16 ``create_payload_index`` round-trips used to run on EVERY embedded
   file (~0.5s/file against a standalone Qdrant). Verified here: second
   call is a no-op; ``reset_client_cache()`` re-arms it.

2. Index pins: ``file_id``/``folder_id`` (chunks) and ``file_ids``/
   ``folder_id`` (images) MUST be payload-indexed. The per-file
   delete-by-filter in ``replace_chunks_for_file`` and every search's ACL
   folder filter scan the whole collection without them — removing these
   entries reintroduces a pipeline that gets slower as the index grows.
   If you're editing the tuples and this test fails, you are probably
   about to ship that regression.
"""

from __future__ import annotations

from voitta_rag_enterprise.services import vector_store as vs


class _CountingClient:
    """Minimal fake capturing ensure-path calls."""

    def __init__(self) -> None:
        self.get_collection_calls = 0
        self.create_collection_calls = 0
        self.create_index_calls = 0

    def get_collection(self, _name):
        self.get_collection_calls += 1
        return object()  # exists

    def create_collection(self, *a, **k) -> None:
        self.create_collection_calls += 1

    def create_payload_index(self, **_k) -> None:
        self.create_index_calls += 1


def _fresh_fake(monkeypatch) -> _CountingClient:
    fake = _CountingClient()
    monkeypatch.setattr(vs, "get_client", lambda: fake)
    vs._ensured_collections.clear()
    return fake


def test_ensure_chunks_cached_per_process(monkeypatch) -> None:
    fake = _fresh_fake(monkeypatch)
    vs.ensure_chunks_collection(text_dim=8)
    first_checks = fake.get_collection_calls
    first_indexes = fake.create_index_calls
    assert first_checks == 1
    assert first_indexes == len(vs._CHUNK_PAYLOAD_INDEXES)

    # Second (and hundredth) call: zero Qdrant traffic.
    for _ in range(100):
        vs.ensure_chunks_collection(text_dim=8)
    assert fake.get_collection_calls == first_checks
    assert fake.create_index_calls == first_indexes


def test_ensure_images_cached_independently(monkeypatch) -> None:
    fake = _fresh_fake(monkeypatch)
    vs.ensure_images_collection(image_dim=8)
    assert vs.IMAGES in vs._ensured_collections
    assert vs.CHUNKS not in vs._ensured_collections  # per-collection keying
    calls = fake.get_collection_calls
    vs.ensure_images_collection(image_dim=8)
    assert fake.get_collection_calls == calls


def test_reset_client_cache_rearms_ensure(monkeypatch) -> None:
    fake = _fresh_fake(monkeypatch)
    vs.ensure_chunks_collection(text_dim=8)
    assert vs.CHUNKS in vs._ensured_collections
    vs.reset_client_cache()
    assert not vs._ensured_collections
    # Re-arm with a fresh fake (reset tore down the worker executor; the
    # next run_on_qdrant lazily rebuilds it).
    fake2 = _CountingClient()
    monkeypatch.setattr(vs, "get_client", lambda: fake2)
    vs.ensure_chunks_collection(text_dim=8)
    assert fake2.get_collection_calls == 1


def test_ensure_not_cached_on_failure(monkeypatch) -> None:
    """A failed ensure must NOT poison the cache — next call retries."""
    fake = _CountingClient()

    def _boom():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(vs, "get_client", _boom)
    vs._ensured_collections.clear()
    try:
        vs.ensure_chunks_collection(text_dim=8)
    except RuntimeError:
        pass
    assert vs.CHUNKS not in vs._ensured_collections
    monkeypatch.setattr(vs, "get_client", lambda: fake)
    vs.ensure_chunks_collection(text_dim=8)
    assert fake.get_collection_calls == 1


# --- index pins --------------------------------------------------------------


def test_chunk_hot_filter_fields_are_indexed() -> None:
    fields = {name for name, _schema in vs._CHUNK_PAYLOAD_INDEXES}
    assert "file_id" in fields, "per-file delete filter needs this index"
    assert "folder_id" in fields, "search ACL filter needs this index"


def test_image_hot_filter_fields_are_indexed() -> None:
    fields = {name for name, _schema in vs._IMAGE_PAYLOAD_INDEXES}
    assert "file_ids" in fields, "image point removal filters on this array"
    assert "folder_id" in fields, "image search ACL filter needs this index"
