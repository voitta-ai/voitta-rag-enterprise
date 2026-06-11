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
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Chunk, File, Folder, Image
from . import events
from .file_classify import bucket_label
from .reconcile import folder_health

logger = logging.getLogger(__name__)

_IN_PROGRESS_STATES = ("extracted", "embedding")


def compute_folder_stats(
    session: Session, folder: Folder, rel_prefix: str | None = None
) -> dict:
    """Return the same dict shape the REST endpoint ships.

    With ``rel_prefix`` set, every count (files, by_extension, chunks,
    images, bytes) is scoped to files under that subdirectory — the same
    ``rel_path.startswith(prefix + "/")`` boundary the SPA's subtree view
    uses. ``index_health`` stays folder-level either way: Qdrant point
    counts are tracked per folder, not per subtree.

    The shape is plain JSON-able primitives so a caller can publish the
    return value as a WebSocket event without a Pydantic round-trip.
    """
    folder_id = folder.id
    files = list(
        session.execute(
            select(File).where(File.folder_id == folder_id, File.state != "deleted")
        ).scalars()
    )
    if rel_prefix:
        files = [f for f in files if f.rel_path.startswith(rel_prefix + "/")]
    files_total = len(files)
    files_indexed = sum(1 for f in files if f.state == "indexed")
    files_error = sum(1 for f in files if f.state == "error")
    files_unsupported = sum(1 for f in files if f.state == "unsupported")
    files_in_progress = sum(1 for f in files if f.state in _IN_PROGRESS_STATES)
    files_pending = sum(1 for f in files if f.state == "pending")
    # Cloud placeholders parked by the extract worker (Drive file with no
    # local bytes) — a subset of unsupported, surfaced separately so the UI
    # can say "N cloud-only (waiting for Google Drive)" instead of lumping
    # them in with genuinely unparseable files.
    files_cloud_only = sum(
        1
        for f in files
        if f.state == "unsupported" and (f.error or "").startswith("cloud-only")
    )
    bytes_total = sum(f.size_bytes or 0 for f in files)
    file_ids = [f.id for f in files]

    chunks_by_file: dict[int, int] = {}
    if file_ids:
        chunks_by_file = dict(
            session.execute(
                select(Chunk.file_id, func.count(Chunk.id))
                .where(Chunk.file_id.in_(file_ids))
                .group_by(Chunk.file_id)
            ).all()
        )

    by_extension: dict[str, dict[str, int]] = {}
    for f in files:
        ext = bucket_label(f)
        es = by_extension.setdefault(
            ext,
            {
                "files": 0,
                "indexed": 0,
                "error": 0,
                "unsupported": 0,
                "pending": 0,
                "in_progress": 0,
                "chunks": 0,
            },
        )
        es["files"] += 1
        if f.state == "indexed":
            es["indexed"] += 1
        elif f.state == "error":
            es["error"] += 1
        elif f.state == "unsupported":
            es["unsupported"] += 1
        elif f.state in _IN_PROGRESS_STATES:
            es["in_progress"] += 1
        else:
            es["pending"] += 1
        es["chunks"] += chunks_by_file.get(f.id, 0)

    chunks_total = sum(chunks_by_file.values())
    images_total = (
        session.execute(
            select(func.count(Image.id)).where(Image.file_id.in_(file_ids))
        ).scalar_one()
        if file_ids
        else 0
    )
    images_unique = (
        session.execute(
            select(func.count(func.distinct(Image.image_cas_id))).where(
                Image.file_id.in_(file_ids)
            )
        ).scalar_one()
        if file_ids
        else 0
    )
    health = folder_health(session, folder)

    return {
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
        "index_health": {
            "status": health.status,
            "qdrant_chunk_points": int(health.qdrant_chunk_points),
        },
    }


def publish_folder_stats(session: Session, folder_id: int) -> None:
    """Compute + emit ``folder.stats_changed`` for the given folder.

    No-op if the folder row is gone. Errors don't propagate — the
    publisher is called from the indexer's hot path (``commit_indexing``,
    ``wipe_file_data``, ``_decrement_pending_embeds``) and a metrics
    glitch must not break the actual indexing pipeline.
    """
    try:
        folder = session.get(Folder, folder_id)
        if folder is None:
            return
        stats = compute_folder_stats(session, folder)
        events.publish(
            "folders",
            {"type": "folder.stats_changed", "folder_id": folder_id, "stats": stats},
        )
    except Exception:
        logger.exception("publish_folder_stats failed for folder=%s", folder_id)
