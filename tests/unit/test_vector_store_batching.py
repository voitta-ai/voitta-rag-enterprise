"""Chunk upserts are batched so a file that produces thousands of chunks
(a large spreadsheet — one chunk per row) doesn't exceed Qdrant's per-request
size limit (default 32 MB), which previously failed the whole file with a
400 ``payload ... larger than allowed``.
"""

from __future__ import annotations

import math

from voitta_rag_enterprise.services import vector_store as vs
from voitta_rag_enterprise.services.embedding import SparseVector
from voitta_rag_enterprise.services.vector_store import (
    _DELETE_FILTER_BATCH,
    _UPSERT_BATCH_POINTS,
    ChunkPoint,
    delete_chunks_for_files,
    replace_chunks_for_file,
)


class _FakeClient:
    def __init__(self) -> None:
        self.upsert_batch_sizes: list[int] = []
        self.upsert_waits: list[bool] = []
        self.deletes = 0

    def delete(self, *a, **k) -> None:
        self.deletes += 1

    def upsert(self, _collection, *, points, wait=True) -> None:
        self.upsert_batch_sizes.append(len(points))
        self.upsert_waits.append(bool(wait))


def _point(i: int) -> ChunkPoint:
    return ChunkPoint(
        chunk_id=i,
        file_id=42,
        folder_id=1,
        file_path="big.xlsx",
        chunk_index=i,
        text=f"row {i}",
        dense=[0.0, 1.0, 2.0],
        sparse=SparseVector(indices=[i], values=[1.0]),
        nearby_image_ids=[],
        source_url=None,
        tab=None,
        dense_model_version="d@1",
        sparse_model_version="s@1",
        allowed_users=[7],
    )


def _run(monkeypatch, n: int) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(vs, "get_client", lambda: fake)
    replace_chunks_for_file(42, [_point(i) for i in range(n)])
    return fake


def test_upsert_batched_for_large_file(monkeypatch) -> None:
    n = _UPSERT_BATCH_POINTS * 5 + 17  # spans several batches + a remainder
    fake = _run(monkeypatch, n)
    # Delete-then-upsert; upsert split into ceil(n / batch) calls.
    assert fake.deletes == 1
    assert len(fake.upsert_batch_sizes) == math.ceil(n / _UPSERT_BATCH_POINTS)
    # No batch exceeds the cap, and every point is upserted exactly once.
    assert all(sz <= _UPSERT_BATCH_POINTS for sz in fake.upsert_batch_sizes)
    assert sum(fake.upsert_batch_sizes) == n
    # Interior batches are fire-and-forget; ONLY the final batch waits —
    # its ack is the durability barrier before the caller flips file state.
    assert fake.upsert_waits[:-1] == [False] * (len(fake.upsert_waits) - 1)
    assert fake.upsert_waits[-1] is True


def test_small_file_single_upsert(monkeypatch) -> None:
    fake = _run(monkeypatch, 10)
    assert fake.upsert_batch_sizes == [10]
    # Single-batch files (the common case) always wait — the "indexed ⇒
    # vectors durably applied" invariant is untouched for them.
    assert fake.upsert_waits == [True]


def test_no_points_skips_upsert(monkeypatch) -> None:
    fake = _run(monkeypatch, 0)
    assert fake.deletes == 1  # stale points still cleared
    assert fake.upsert_batch_sizes == []


# --- delete_chunks_for_files: partial reindex must NOT wipe the folder -------


class _FakeDeleteClient:
    """Records the file_id sets passed to each delete filter."""

    def __init__(self) -> None:
        self.deleted_file_id_batches: list[list[int]] = []

    def collection_exists(self, _c) -> bool:
        return True

    def delete(self, _collection, *, points_selector) -> None:
        cond = points_selector.filter.must[0]
        # MatchAny carries the file_ids this delete is scoped to.
        self.deleted_file_id_batches.append(list(cond.match.any))


def test_delete_scoped_to_given_file_ids(monkeypatch) -> None:
    """The reindex wipe targets exactly the passed file_ids (file-scoped),
    not a whole folder — this is the bug fix: a partial reindex used to
    delete every file's points in the folder."""
    fake = _FakeDeleteClient()
    monkeypatch.setattr(vs, "get_client", lambda: fake)
    delete_chunks_for_files([10, 20, 30])
    assert fake.deleted_file_id_batches == [[10, 20, 30]]


def test_delete_batched_for_whole_folder(monkeypatch) -> None:
    """A full-folder reindex passes thousands of ids — they're split into
    bounded MatchAny filters, not one giant request."""
    fake = _FakeDeleteClient()
    monkeypatch.setattr(vs, "get_client", lambda: fake)
    ids = list(range(_DELETE_FILTER_BATCH * 2 + 3))
    delete_chunks_for_files(ids)
    assert len(fake.deleted_file_id_batches) == 3
    assert all(len(b) <= _DELETE_FILTER_BATCH for b in fake.deleted_file_id_batches)
    assert sorted(i for b in fake.deleted_file_id_batches for i in b) == ids


def test_delete_empty_is_noop(monkeypatch) -> None:
    fake = _FakeDeleteClient()
    monkeypatch.setattr(vs, "get_client", lambda: fake)
    delete_chunks_for_files([])
    assert fake.deleted_file_id_batches == []
