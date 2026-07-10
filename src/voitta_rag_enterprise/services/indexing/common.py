"""Shared indexing primitives: job-progress/stage helpers, file-state
mutation + event publishing, and small utilities used across the package."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from contextlib import contextmanager

from sqlalchemy import select

from ...db.database import session_scope
from ...db.models import File, Image
from .. import events

logger = logging.getLogger(__name__)

_ERROR_FIELD_MAX = 4000  # cap stored traceback so an error row stays scannable

# Process-wide lock around the extract pipeline.
#
# Originally added because PyMuPDF / cairo / some Pillow decoders are not
# fully thread-safe at the C level and N parallel extracts produced glibc
# heap corruption. The default worker pool is now size=1 (see
# settings.resolved_workers), so two workers cannot collide here even
# without the lock.
#
# The lock is still necessary because ``wipe_file_data`` runs from a REST
# handler thread (the /reindex endpoint) and must not race with the
# worker thread mid-extract: the worker reads + replaces image rows for
# a file, and a concurrent wipe in another thread would either delete
# stale rows by id (no-op) or leave the new rows in place (the bug
# users see as "reindex did nothing"). Holding _EXTRACT_LOCK across the
# wipe waits out any in-flight extract before deleting.
#
# gpu_lock (services/gpu_lock.py) is a separate, finer-grained lock that
# also serializes search query embeds against indexing — search runs on
# the asyncio loop, not the worker, so this is the only thing keeping
# query and indexing from racing on the GPU.
_EXTRACT_LOCK = threading.Lock()


def _publish_job_progress(phase: str, done: int | None = None, total: int | None = None) -> None:
    """Emit a transient ``job.progress`` for the job bound on the current
    context (the worker binds ``job_id`` around each handler), so the Jobs
    panel can show what a long extract is doing right now. No-op outside a job
    (e.g. REST-triggered reindex scans). Coalesced per job_id by the broker.
    """
    from ...logging_config import current_context_value

    job_id = current_context_value("job_id")
    if job_id is None:
        return
    event: dict = {"type": "job.progress", "job_id": job_id, "phase": phase}
    if total is not None:
        event["done"] = done
        event["total"] = total
    events.publish("jobs", event)


@contextmanager
def _stage(name: str, **extra):
    """Log entry/exit + elapsed ms for a single indexing stage, and surface the
    stage name as live job sub-progress (Jobs panel)."""
    _publish_job_progress(name)
    start = time.perf_counter()
    if extra:
        logger.debug("stage %s start %s", name, extra)
    else:
        logger.debug("stage %s start", name)
    try:
        yield
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception("stage %s failed after %.1fms", name, elapsed_ms)
        raise
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug("stage %s done in %.1fms", name, elapsed_ms)


def _format_exception(prefix: str) -> str:
    """Build the string we put into File.error — prefix + tail of traceback."""
    tb = traceback.format_exc()
    msg = f"{prefix}\n{tb}"
    if len(msg) > _ERROR_FIELD_MAX:
        msg = msg[: _ERROR_FIELD_MAX - 3] + "..."
    return msg


def _mark_state(file_id: int, *, state: str, error: str | None = None) -> None:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        file.state = state
        file.error = error
    publish_file_upserted(file_id)


def publish_file_upserted(file_id: int) -> None:
    """Publish ``file.upserted`` + ``folder.stats_changed`` for the SPA.

    Single source of truth — every call site that mutates a ``File`` row
    must call this (or ``events.publish('files', {'type': 'file.deleted',
    ...})`` for terminal removals). Reads the row back from the DB inside
    a fresh session so the payload reflects the committed state, not
    whatever the caller happens to have in memory. No-op if the row
    vanished between the mutation and this call.

    The same hook publishes ``folder.stats_changed`` so the sidebar's
    chunk/image/byte counters stay in lockstep with the files store.
    Both events go out in the same session — they reflect the same
    committed snapshot. ``events.py`` coalesces the snapshots per
    folder_id, so a 200-file extract burst produces one delivered
    stats event per folder, not 200.
    """
    from ..folder_stats import publish_folder_stats

    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        events.publish(
            "files",
            {
                "type": "file.upserted",
                "file": file_event_payload(file),
            },
        )
        publish_folder_stats(s, file.folder_id)


def file_event_payload(file: File, *, image_count: int | None = None) -> dict:
    """Shape the SPA expects on every ``file.upserted`` event.

    ``image_count`` lets the file tree decide expandability up front — a docx /
    xlsx with no extracted images shows no expand chevron at all (rather than
    expanding to an empty "No previews"). Pass it precomputed (the snapshot
    builder counts in bulk to avoid N+1); otherwise it's counted inline from the
    file's own session.
    """
    if image_count is None:
        from sqlalchemy import func
        from sqlalchemy.orm import object_session

        sess = object_session(file)
        image_count = (
            sess.execute(
                select(func.count()).select_from(Image).where(Image.file_id == file.id)
            ).scalar_one()
            if sess is not None
            else 0
        )
    # Per-file source provenance (owner / editor / shared_by + created/modified
    # epochs) so the file-preview panel can show it. Parsed from the
    # File.source_meta JSON; None for non-synced files.
    provenance: dict | None = None
    if file.source_meta:
        import json as _json

        try:
            provenance = _json.loads(file.source_meta)
        except (ValueError, TypeError):
            provenance = None

    return {
        "id": file.id,
        "folder_id": file.folder_id,
        "rel_path": file.rel_path,
        "state": file.state,
        "size_bytes": file.size_bytes,
        "mtime_ns": file.mtime_ns,
        "added_at": file.added_at,
        "last_indexed_at": file.last_indexed_at,
        "pending_embeds": file.pending_embeds,
        "source_url": file.source_url,
        "tab": file.tab,
        "image_count": image_count,
        "provenance": provenance,
    }


def _mark_error(file_id: int, message: str) -> None:
    logger.warning("extract %d failed: %s", file_id, message)
    _mark_state(file_id, state="error", error=message)


def _chunked(seq: list[int], size: int):
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
