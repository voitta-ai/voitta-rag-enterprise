"""Qdrant adapter — collection bootstrap, upserts, search.

See ARCHITECTURE.md §3.4 for the named-vector layout and payload schema.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import get_settings
from .embedding import SparseVector

logger = logging.getLogger(__name__)

CHUNKS = "chunks"
IMAGES = "images"

_client: QdrantClient | None = None
_lock = threading.Lock()
# QdrantLocal's underlying SQLite connection is pinned to the thread that
# created it. To stay thread-safe we route every Qdrant call through a single
# dedicated worker thread. ``run_on_qdrant`` submits a callable and blocks for
# its result.
_executor: ThreadPoolExecutor | None = None
_T = TypeVar("_T")


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="qdrant-worker"
        )
    return _executor


def run_on_qdrant(fn) -> Any:
    """Run ``fn()`` on the dedicated Qdrant thread; return its result.

    Reraises any exception from ``fn`` in the calling thread.
    """
    return _ensure_executor().submit(fn).result()


def get_client() -> QdrantClient:
    """Return the shared QdrantClient. Must be called from the qdrant worker thread.

    The local backend creates a SQLite connection bound to whichever thread first
    instantiates it, which is why every Qdrant call goes through ``run_on_qdrant``.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                settings = get_settings()
                if settings.qdrant_url:
                    _client = QdrantClient(url=settings.qdrant_url)
                else:
                    path = settings.resolved_qdrant_path()
                    path.mkdir(parents=True, exist_ok=True)
                    _client = QdrantClient(path=str(path))
    return _client


def reset_client_cache() -> None:
    global _client, _executor
    if _executor is not None:
        # Close the client on its own thread, then tear down the executor.
        if _client is not None:
            client = _client

            def _close() -> None:
                with contextlib.suppress(Exception):
                    client.close()

            with contextlib.suppress(Exception):
                _executor.submit(_close).result(timeout=5)
        _executor.shutdown(wait=True)
        _executor = None
    elif _client is not None:
        with contextlib.suppress(Exception):
            _client.close()
    _client = None


def ensure_chunks_collection(text_dim: int) -> None:
    """Idempotent — only the chunks (text) collection."""

    def _do() -> None:
        client = get_client()
        if not _collection_exists(client, CHUNKS):
            client.create_collection(
                CHUNKS,
                vectors_config={
                    "dense": qm.VectorParams(size=text_dim, distance=qm.Distance.COSINE),
                },
                sparse_vectors_config={
                    "sparse": qm.SparseVectorParams(),
                },
            )
            logger.info("created qdrant collection: %s (dense dim=%d)", CHUNKS, text_dim)

    run_on_qdrant(_do)


def ensure_images_collection(image_dim: int) -> None:
    """Idempotent — only the images collection."""

    def _do() -> None:
        client = get_client()
        if not _collection_exists(client, IMAGES):
            client.create_collection(
                IMAGES,
                vectors_config={
                    "image": qm.VectorParams(size=image_dim, distance=qm.Distance.COSINE),
                },
            )
            logger.info("created qdrant collection: %s (dim=%d)", IMAGES, image_dim)

    run_on_qdrant(_do)


def ensure_collections(text_dim: int, image_dim: int) -> None:
    """Convenience wrapper — bootstrap both collections in one call."""
    ensure_chunks_collection(text_dim)
    ensure_images_collection(image_dim)


def _collection_exists(client: QdrantClient, name: str) -> bool:
    try:
        client.get_collection(name)
        return True
    except Exception:
        return False


def count_points_for_folder(collection: str, folder_id: int) -> int:
    """Return the number of points in ``collection`` whose payload's
    ``folder_id`` matches. Used by the reconcile health check.

    Returns 0 when the collection doesn't exist (fresh deployment, never
    embedded). Approximate counting is fine — we use it to flag big
    SQLite-vs-Qdrant gaps, not for exact bookkeeping.
    """

    def _do() -> int:
        client = get_client()
        if not _collection_exists(client, collection):
            return 0
        result = client.count(
            collection,
            count_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="folder_id", match=qm.MatchValue(value=folder_id)
                    )
                ]
            ),
            exact=False,
        )
        return int(result.count)

    return run_on_qdrant(_do)


@dataclass
class ChunkPoint:
    chunk_id: int
    file_id: int
    folder_id: int
    file_path: str
    chunk_index: int
    text: str
    dense: list[float]
    sparse: SparseVector
    nearby_image_ids: list[int]
    source_url: str | None
    tab: str | None
    dense_model_version: str
    sparse_model_version: str
    allowed_users: list[int]


@dataclass
class ImagePoint:
    point_id: int
    image_cas_id: str
    file_id: int
    folder_id: int
    file_path: str
    anchor_chunk: int | None
    page: int | None
    image: list[float]
    image_model_version: str
    allowed_users: list[int]


def replace_chunks_for_file(file_id: int, points: list[ChunkPoint]) -> None:
    """Atomic: drop every point owned by ``file_id``, then upsert ``points``."""

    def _do() -> None:
        client = get_client()
        client.delete(
            CHUNKS,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="file_id", match=qm.MatchValue(value=file_id)
                        )
                    ]
                )
            ),
        )
        if not points:
            return
        client.upsert(
            CHUNKS,
            points=[
                qm.PointStruct(
                    id=p.chunk_id,
                    vector={
                        "dense": p.dense,
                        "sparse": qm.SparseVector(
                            indices=p.sparse.indices, values=p.sparse.values
                        ),
                    },
                    payload={
                        "chunk_id": p.chunk_id,
                        "file_id": p.file_id,
                        "folder_id": p.folder_id,
                        "file_path": p.file_path,
                        "chunk_index": p.chunk_index,
                        "text": p.text,
                        "nearby_image_ids": p.nearby_image_ids,
                        "source_url": p.source_url,
                        "tab": p.tab,
                        "dense_model_version": p.dense_model_version,
                        "sparse_model_version": p.sparse_model_version,
                        "allowed_users": p.allowed_users,
                    },
                )
                for p in points
            ],
        )

    run_on_qdrant(_do)


def find_image_point_by_cas(image_cas_id: str) -> dict[str, Any] | None:
    """Return the existing image point payload, or ``None`` if not indexed yet."""

    def _do() -> dict[str, Any] | None:
        client = get_client()
        res, _ = client.scroll(
            IMAGES,
            scroll_filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="image_cas_id", match=qm.MatchValue(value=image_cas_id)
                    )
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if not res:
            return None
        p = res[0]
        return {"id": p.id, "payload": dict(p.payload or {})}

    return run_on_qdrant(_do)


def add_file_to_image_point(point_id: int, file_id: int) -> None:
    """Append ``file_id`` to an existing image point's ``file_ids`` list."""

    def _do() -> None:
        client = get_client()
        existing = client.retrieve(IMAGES, ids=[point_id], with_payload=True)
        if not existing:
            return
        payload = dict(existing[0].payload or {})
        file_ids = list(payload.get("file_ids") or [])
        if file_id not in file_ids:
            file_ids.append(file_id)
            client.set_payload(
                IMAGES, payload={"file_ids": file_ids}, points=[point_id]
            )

    run_on_qdrant(_do)


def upsert_image_point(point: ImagePoint, file_ids: list[int]) -> None:
    def _do() -> None:
        client = get_client()
        client.upsert(
            IMAGES,
            points=[
                qm.PointStruct(
                    id=point.point_id,
                    vector={"image": point.image},
                    payload={
                        "image_id": point.point_id,
                        "image_cas_id": point.image_cas_id,
                        "file_ids": file_ids,
                        "folder_id": point.folder_id,
                        "file_path": point.file_path,
                        "anchor_chunk": point.anchor_chunk,
                        "page": point.page,
                        "image_model_version": point.image_model_version,
                        "allowed_users": point.allowed_users,
                    },
                )
            ],
        )

    run_on_qdrant(_do)


def remove_file_from_image_points(file_id: int) -> list[int]:
    """Drop ``file_id`` from every image point's ``file_ids``; delete points that empty.

    Returns the list of point ids that were deleted. No-op (returns []) if
    the ``images`` collection has not been created yet — that just means we
    have nothing to clean up.
    """

    def _do() -> list[int]:
        client = get_client()
        if not client.collection_exists(IMAGES):
            return []
        res, _ = client.scroll(
            IMAGES,
            scroll_filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="file_ids", match=qm.MatchValue(value=file_id))
                ]
            ),
            limit=10_000,
            with_payload=True,
        )
        deleted: list[int] = []
        for p in res:
            payload = dict(p.payload or {})
            file_ids = [fid for fid in (payload.get("file_ids") or []) if fid != file_id]
            if file_ids:
                client.set_payload(IMAGES, payload={"file_ids": file_ids}, points=[p.id])
            else:
                client.delete(IMAGES, points_selector=qm.PointIdsList(points=[p.id]))
                deleted.append(p.id)
        return deleted

    return run_on_qdrant(_do)


def delete_chunks_for_file(file_id: int) -> None:
    def _do() -> None:
        client = get_client()
        if not client.collection_exists(CHUNKS):
            return
        client.delete(
            CHUNKS,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="file_id", match=qm.MatchValue(value=file_id)
                        )
                    ]
                )
            ),
        )

    run_on_qdrant(_do)


def delete_chunks_for_folder(folder_id: int) -> None:
    """Drop every chunk point that belongs to ``folder_id``.

    Used by ``reindex_folder`` to wipe an entire folder's chunk index in a
    single Qdrant round-trip instead of N (one per file). Folder-scope
    reindex was the slow path that motivated this — N=2000 individual
    filter-deletes took 6 minutes vs ~50 ms here.
    """

    def _do() -> None:
        client = get_client()
        if not client.collection_exists(CHUNKS):
            return
        client.delete(
            CHUNKS,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="folder_id", match=qm.MatchValue(value=folder_id)
                        )
                    ]
                )
            ),
        )

    run_on_qdrant(_do)


def remove_files_from_image_points(file_ids: list[int]) -> int:
    """Bulk version of ``remove_file_from_image_points``.

    Image points are CAS-deduped and shared across files (their ``file_ids``
    payload is a list), so we can't just filter-delete by folder_id like
    chunks. Instead we scroll points whose ``file_ids`` intersects the
    target set, edit each payload in memory, and either re-upsert the
    trimmed ``file_ids`` or delete the point if no references remain.

    Returns the number of points that ended up fully deleted (for stats).
    Does ONE scroll over the matching subset and one Qdrant call per
    affected point, vs the old code's per-file scroll+update loop.
    """
    if not file_ids:
        return 0
    target = set(file_ids)

    def _do() -> int:
        client = get_client()
        if not client.collection_exists(IMAGES):
            return 0
        # ``MatchAny`` on an array field matches points whose array contains
        # at least one of the listed values — exactly what we want.
        res, _ = client.scroll(
            IMAGES,
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="file_ids", match=qm.MatchAny(any=list(target)))]
            ),
            limit=100_000,
            with_payload=True,
        )
        deleted = 0
        to_delete: list[int] = []
        for p in res:
            payload = dict(p.payload or {})
            remaining = [fid for fid in (payload.get("file_ids") or []) if fid not in target]
            if remaining:
                client.set_payload(IMAGES, payload={"file_ids": remaining}, points=[p.id])
            else:
                to_delete.append(p.id)
        if to_delete:
            client.delete(IMAGES, points_selector=qm.PointIdsList(points=to_delete))
            deleted = len(to_delete)
        return deleted

    return run_on_qdrant(_do)


@dataclass
class SearchHit:
    id: int
    score: float
    payload: dict[str, Any]


def search_chunks(
    dense: list[float],
    sparse: SparseVector,
    *,
    limit: int = 20,
    folder_ids: list[int] | None = None,
    allowed_user_id: int | None = None,
) -> list[SearchHit]:
    """Hybrid dense + sparse search over the ``chunks`` collection (RRF-fused)."""
    flt = _build_filter(folder_ids=folder_ids, allowed_user_id=allowed_user_id)

    def _do() -> tuple[list[Any], list[Any]]:
        client = get_client()
        dense_resp = client.query_points(
            CHUNKS,
            query=dense,
            using="dense",
            limit=limit,
            query_filter=flt,
            with_payload=True,
        )
        sparse_points: list[Any] = []
        if sparse.indices:
            sparse_resp = client.query_points(
                CHUNKS,
                query=qm.SparseVector(indices=sparse.indices, values=sparse.values),
                using="sparse",
                limit=limit,
                query_filter=flt,
                with_payload=True,
            )
            sparse_points = list(sparse_resp.points)
        return list(dense_resp.points), sparse_points

    dense_points, sparse_points = run_on_qdrant(_do)
    fused = _rrf([dense_points, sparse_points], k=60, limit=limit)
    return [SearchHit(id=p.id, score=p.score, payload=dict(p.payload or {})) for p in fused]


def search_images(
    vector: list[float],
    *,
    limit: int = 20,
    folder_ids: list[int] | None = None,
    allowed_user_id: int | None = None,
) -> list[SearchHit]:
    flt = _build_filter(folder_ids=folder_ids, allowed_user_id=allowed_user_id)

    def _do() -> list[Any]:
        client = get_client()
        resp = client.query_points(
            IMAGES,
            query=vector,
            using="image",
            limit=limit,
            query_filter=flt,
            with_payload=True,
        )
        return list(resp.points)

    points = run_on_qdrant(_do)
    return [SearchHit(id=p.id, score=p.score, payload=dict(p.payload or {})) for p in points]


def _build_filter(
    *,
    folder_ids: list[int] | None = None,
    allowed_user_id: int | None = None,
) -> qm.Filter | None:
    must: list[qm.FieldCondition] = []
    if folder_ids:
        must.append(
            qm.FieldCondition(key="folder_id", match=qm.MatchAny(any=list(folder_ids)))
        )
    if allowed_user_id is not None:
        must.append(
            qm.FieldCondition(
                key="allowed_users", match=qm.MatchValue(value=allowed_user_id)
            )
        )
    return qm.Filter(must=must) if must else None


def _rrf(rank_lists: list[list[Any]], *, k: int = 60, limit: int = 20) -> list[Any]:
    """Reciprocal Rank Fusion. Returns merged points sorted by fused score."""
    scores: dict[int, float] = {}
    objs: dict[int, Any] = {}
    for lst in rank_lists:
        for rank, p in enumerate(lst):
            scores[p.id] = scores.get(p.id, 0.0) + 1.0 / (k + rank + 1)
            objs[p.id] = p
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out = []
    for pid, s in ranked:
        p = objs[pid]
        # Replace the original score with the fused score for downstream display.
        p.score = s
        out.append(p)
    return out
