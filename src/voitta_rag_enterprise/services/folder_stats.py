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

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Chunk, File, Folder, Image
from . import events
from .file_classify import bucket_label
from .reconcile import folder_health

logger = logging.getLogger(__name__)

_IN_PROGRESS_STATES = ("extracted", "embedding")


def _aggregate_provenance(files: list[File]) -> dict | None:
    """Roll up source-object provenance (File.source_meta) across a folder.

    For the Details pane: who shared the synced root (uniform when downfilled),
    the distinct owners with file counts (top few), and the created/modified
    date range. Returns None when no file carries provenance (non-synced
    folders / not-yet-reindexed). All timestamps are epoch seconds.
    """
    owners: dict[str, dict] = {}
    shared_by: dict[str, str] = {}
    created_min = modified_max = None
    any_meta = False
    for f in files:
        if not f.source_meta:
            continue
        try:
            sm = json.loads(f.source_meta)
        except (ValueError, TypeError):
            continue
        if not isinstance(sm, dict):
            continue
        any_meta = True
        email = sm.get("owner_email") or sm.get("owner_name")
        if email:
            o = owners.setdefault(email, {"email": sm.get("owner_email") or "",
                                          "name": sm.get("owner_name") or "", "count": 0})
            o["count"] += 1
        if sm.get("shared_by_email") or sm.get("shared_by_name"):
            shared_by = {"email": sm.get("shared_by_email") or "",
                         "name": sm.get("shared_by_name") or ""}
        c = sm.get("created_ts")
        if isinstance(c, int):
            created_min = c if created_min is None else min(created_min, c)
        m = sm.get("modified_ts")
        if isinstance(m, int):
            modified_max = m if modified_max is None else max(modified_max, m)
    if not any_meta:
        return None
    top_owners = sorted(owners.values(), key=lambda o: o["count"], reverse=True)[:6]
    return {
        "shared_by": shared_by or None,
        "owners": top_owners,
        "owner_count": len(owners),
        "created_min": created_min,
        "modified_max": modified_max,
    }


def compute_folder_stats(session: Session, folder: Folder) -> dict:
    """Return the same dict shape the REST endpoint ships.

    The shape is plain JSON-able primitives so a caller can publish the
    return value as a WebSocket event without a Pydantic round-trip.
    """
    folder_id = folder.id
    files = list(
        session.execute(
            select(File).where(File.folder_id == folder_id, File.state != "deleted")
        ).scalars()
    )
    files_total = len(files)
    files_indexed = sum(1 for f in files if f.state == "indexed")
    files_error = sum(1 for f in files if f.state == "error")
    files_unsupported = sum(1 for f in files if f.state == "unsupported")
    files_in_progress = sum(1 for f in files if f.state in _IN_PROGRESS_STATES)
    files_pending = sum(1 for f in files if f.state == "pending")
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
        "chunks_total": int(chunks_total),
        "images_total": int(images_total),
        "images_unique": int(images_unique),
        "bytes_total": int(bytes_total),
        "by_extension": by_extension,
        "provenance": _aggregate_provenance(files),
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
