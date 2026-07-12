"""Folder stats — single source of truth for sidebar counts.

The same payload the REST endpoint ``GET /api/folders/{id}/stats``
returns is also published over the WebSocket as a
``folder.stats_changed`` event whenever any artifact in the folder is
committed. The SPA's ``folderStats`` store keeps the latest snapshot per
folder and the sidebar reads from it directly — no more "files counts
update live but chunks/images go stale until something else refreshes".

This module is the pure-data computation; it's framework-agnostic so we
can call it from the REST handler (with HTTPException-aware permission
checks) AND from the indexer's commit hooks (no auth context, just a
folder id).

Performance model (why the debounce + cheap-compute exist)
----------------------------------------------------------
Indexing a folder fires ``publish_file_upserted`` per file, which used to
recompute whole-folder stats **synchronously, per file** — O(N^2) for a
folder, and heavy enough (ORM hydration of every row + ``IN (all ids)``
aggregates + a Qdrant point count) to pin the GIL and starve the single
indexer worker. Two mechanisms fix that:

* ``compute_folder_stats`` reads lightweight column tuples and aggregates
  chunks/images via JOINs (no giant ``IN`` list, no ORM hydration), and
  can skip the Qdrant health count (``include_health=False``).
* The hot path calls ``mark_folder_stats_dirty`` instead of computing
  inline; a background flusher (``run_stats_flusher``) coalesces a burst
  into at most one compute+publish per folder per interval. When no
  flusher is running (tests, ``VOITTA_DISABLE_BACKGROUND``) mark-dirty
  falls back to a synchronous publish so behaviour is unchanged there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.database import session_scope
from ..db.models import Chunk, File, Folder, Image
from . import events
from .file_classify import bucket_label_for
from .reconcile import folder_health

logger = logging.getLogger(__name__)

_IN_PROGRESS_STATES = ("extracted", "embedding")


def _file_filter(folder_id: int, rel_prefix: str | None):
    """Shared WHERE for the row read AND the JOIN aggregates, so both scope
    to exactly the same file set.

    ``rel_prefix`` uses a ``LIKE`` with the ``%``/``_``/``\\`` metacharacters
    escaped, so a subdir literally containing ``_`` (common in filenames)
    can't over-match — mirrors the old ``rel_path.startswith(prefix + '/')``.
    """
    conds = [File.folder_id == folder_id, File.state != "deleted"]
    if rel_prefix:
        esc = (
            rel_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        conds.append(File.rel_path.like(esc + "/%", escape="\\"))
    return conds


def compute_folder_stats(
    session: Session,
    folder: Folder,
    rel_prefix: str | None = None,
    *,
    include_health: bool = True,
) -> dict:
    """Return the same dict shape the REST endpoint ships.

    With ``rel_prefix`` set, every count (files, by_extension, chunks,
    images, bytes) is scoped to files under that subdirectory. ``index_health``
    stays folder-level either way: Qdrant point counts are tracked per folder,
    not per subtree.

    ``include_health=False`` omits the ``index_health`` key entirely (and the
    Qdrant point count it needs) — used by the live WS push, where the health
    badge is a slow diagnostic served on-demand by the REST endpoint instead.
    The frontend guards the key's absence. The shape is plain JSON-able
    primitives so a caller can publish it as a WebSocket event with no
    Pydantic round-trip.
    """
    folder_id = folder.id
    where = _file_filter(folder_id, rel_prefix)

    # Lightweight column tuples — NOT full ORM objects. Avoids hydrating every
    # row through the identity map (the dominant cost at 12k+ files).
    rows = session.execute(
        select(
            File.id,
            File.state,
            File.source_url,
            File.rel_path,
            File.size_bytes,
            File.error,
        ).where(*where)
    ).all()

    files_total = len(rows)
    files_indexed = files_error = files_unsupported = 0
    files_in_progress = files_pending = files_cloud_only = 0
    bytes_total = 0
    by_extension: dict[str, dict[str, int]] = {}
    # file id → its bucket label, built in the single pass below and reused for
    # per-file chunk attribution (compute the label once per file, not twice).
    ext_of_file: dict[int, str] = {}

    for r in rows:
        state = r.state
        bytes_total += r.size_bytes or 0
        if state == "indexed":
            files_indexed += 1
        elif state == "error":
            files_error += 1
        elif state == "unsupported":
            files_unsupported += 1
            # Cloud placeholders parked by the extract worker (Drive file with
            # no local bytes) — a subset of unsupported, surfaced separately.
            if (r.error or "").startswith("cloud-only"):
                files_cloud_only += 1
        elif state in _IN_PROGRESS_STATES:
            files_in_progress += 1
        else:
            files_pending += 1

        ext = bucket_label_for(r.source_url, r.rel_path)
        ext_of_file[r.id] = ext
        es = by_extension.setdefault(
            ext,
            {
                "files": 0, "indexed": 0, "error": 0, "unsupported": 0,
                "pending": 0, "in_progress": 0, "chunks": 0,
            },
        )
        es["files"] += 1
        if state == "indexed":
            es["indexed"] += 1
        elif state == "error":
            es["error"] += 1
        elif state == "unsupported":
            es["unsupported"] += 1
        elif state in _IN_PROGRESS_STATES:
            es["in_progress"] += 1
        else:
            es["pending"] += 1

    # Per-file chunk counts via JOIN (covering index on chunks(file_id)) —
    # no ``IN (all file ids)`` list. Attribute each file's chunks to its ext
    # via the label already computed in the pass above.
    chunks_by_file: dict[int, int] = dict(
        session.execute(
            select(Chunk.file_id, func.count(Chunk.id))
            .join(File, Chunk.file_id == File.id)
            .where(*where)
            .group_by(Chunk.file_id)
        ).all()
    )
    for fid, n in chunks_by_file.items():
        ext = ext_of_file.get(fid)
        if ext is not None:
            by_extension[ext]["chunks"] += n
    chunks_total = sum(chunks_by_file.values())

    images_total = session.execute(
        select(func.count(Image.id)).join(File, Image.file_id == File.id).where(*where)
    ).scalar_one()
    images_unique = session.execute(
        select(func.count(func.distinct(Image.image_cas_id)))
        .join(File, Image.file_id == File.id)
        .where(*where)
    ).scalar_one()

    out = {
        "folder_id": folder_id,
        "files_total": files_total,
        "files_indexed": files_indexed,
        "files_error": files_error,
        "files_unsupported": files_unsupported,
        "files_in_progress": files_in_progress,
        "files_pending": files_pending,
        "files_cloud_only": files_cloud_only,
        "dir": rel_prefix,
        "chunks_total": int(chunks_total),
        "images_total": int(images_total),
        "images_unique": int(images_unique),
        "bytes_total": int(bytes_total),
        "by_extension": by_extension,
    }
    if include_health:
        health = folder_health(session, folder)
        out["index_health"] = {
            "status": health.status,
            "qdrant_chunk_points": int(health.qdrant_chunk_points),
        }
    return out


def publish_folder_stats(
    session: Session, folder_id: int, *, include_health: bool = False
) -> None:
    """Compute + emit ``folder.stats_changed`` for the given folder.

    No-op if the folder row is gone. Errors don't propagate — the publisher
    is called from indexer paths and a metrics glitch must not break the
    pipeline. ``include_health`` defaults False: live pushes carry
    SQLite-only counts (no per-publish Qdrant probe); the health badge is
    served by the on-demand REST endpoint (``include_health=True``).
    """
    try:
        folder = session.get(Folder, folder_id)
        if folder is None:
            return
        stats = compute_folder_stats(session, folder, include_health=include_health)
        events.publish(
            "folders",
            {"type": "folder.stats_changed", "folder_id": folder_id, "stats": stats},
        )
    except Exception:
        logger.exception("publish_folder_stats failed for folder=%s", folder_id)


# ---------------------------------------------------------------------------
# Debounced stats publishing.
#
# The per-file hot path marks a folder dirty (cheap, thread-safe) instead of
# computing inline; ``run_stats_flusher`` coalesces a burst into one
# compute+publish per folder per interval. Without a running flusher (tests /
# VOITTA_DISABLE_BACKGROUND) mark-dirty publishes synchronously, so behaviour
# is identical there.
# ---------------------------------------------------------------------------

_dirty_folders: set[int] = set()
_dirty_lock = threading.Lock()
_flusher_running = False


def mark_folder_stats_dirty(folder_id: int) -> None:
    """Request a (debounced) stats publish for ``folder_id``.

    Thread-safe; called from the indexer worker thread. When the background
    flusher isn't running, publishes synchronously as a fallback."""
    if _flusher_running:
        with _dirty_lock:
            _dirty_folders.add(folder_id)
        return
    # Fallback: no flusher (tests / background disabled) — publish now so the
    # snapshot still lands. Own session; reads committed state.
    with session_scope() as s:
        publish_folder_stats(s, folder_id)


def _drain_dirty() -> list[int]:
    with _dirty_lock:
        if not _dirty_folders:
            return []
        drained = list(_dirty_folders)
        _dirty_folders.clear()
    return drained


def _compute_and_publish(folder_id: int) -> None:
    with session_scope() as s:
        publish_folder_stats(s, folder_id)


async def run_stats_flusher(interval: float = 1.5) -> None:
    """Background loop: every ``interval`` seconds, publish stats for every
    folder marked dirty since the last tick.

    Trailing-edge: the final post-burst state is always published (the folder
    stays dirty until a tick drains it). The DB work runs in a thread so the
    event loop never blocks. Runs until cancelled (lifespan shutdown)."""
    global _flusher_running
    _flusher_running = True
    logger.info("stats flusher started (interval=%.1fs)", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            for folder_id in _drain_dirty():
                try:
                    await asyncio.to_thread(_compute_and_publish, folder_id)
                except Exception:
                    logger.exception(
                        "stats flusher: publish failed for folder=%s", folder_id
                    )
    except asyncio.CancelledError:
        # Final drain so counts aren't left stale at shutdown.
        for folder_id in _drain_dirty():
            with contextlib.suppress(Exception):
                _compute_and_publish(folder_id)
        raise
    finally:
        _flusher_running = False
