"""The ``reindex_folder`` job handler: wipe + reset + re-enqueue matched files."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from ...db.database import session_scope
from ...db.models import Chunk, File, Image, Job
from ...logging_config import bind_context
from .. import events, job_queue
from .common import _EXTRACT_LOCK, _chunked, file_event_payload, logger


async def run_reindex_folder(payload: dict) -> None:
    """Worker handler: wipe + reset + re-enqueue every matched file.

    The REST endpoint enqueues this and returns immediately so the request
    thread isn't blocked behind ``_EXTRACT_LOCK`` waiting for the current
    extract to finish. Once this handler runs, *we* are the extract worker
    — no other extract can be in flight, so wipes are uncontended and the
    state machine transitions cleanly.
    """
    file_ids: list[int] = list(payload.get("file_ids") or [])
    folder_id = payload.get("folder_id")
    if not file_ids:
        return
    with bind_context(folder_id=folder_id):
        logger.info("reindex begin: %d file(s)", len(file_ids))
        await asyncio.to_thread(_run_reindex_sync, folder_id, file_ids)
        logger.info("reindex done: %d file(s)", len(file_ids))


# Wipe progress is broadcast via WebSocket so the SPA can show a live
# "Wiping… 600/1969" pill on the folder card. Batched in chunks of 200 so
# we publish on the order of 10 events for a 2k-file folder, not one per
# file.
_REINDEX_PROGRESS_CHUNK = 200


def _publish_reindex_progress(
    folder_id: int | None,
    *,
    phase: str,
    done: int,
    total: int,
) -> None:
    if folder_id is None:
        return
    events.publish(
        "folders",
        {
            "type": "folder.reindex_progress",
            "folder_id": folder_id,
            "phase": phase,  # "cancelling" | "wiping" | "queueing" | "done"
            "done": done,
            "total": total,
        },
    )


def _run_reindex_sync(folder_id: int | None, file_ids: list[int]) -> None:
    """Synchronous body of run_reindex_folder. Runs in the worker thread.

    Performs three batched phases (each emits a progress event):

    1. **cancelling** — flip queued extract jobs to ``done(superseded)``
       so a pre-reindex enqueue can't fire on a half-wiped file.
    2. **wiping** — bulk-delete every chunk + image row + Qdrant point
       across the target file_ids in just a few round-trips, regardless
       of how many files are involved.
    3. **queueing** — flip every file row back to ``pending`` and enqueue
       a fresh extract.

    Old code did all three per-file in a Python loop (1969 iterations →
    6 minutes for the user's git-test folder). This version uses bulk SQL
    DELETE + folder-scope Qdrant deletes, taking each phase from O(N)
    round-trips down to O(1).
    """
    from sqlalchemy import delete as sa_delete

    from ...cas import store as cas_store
    from .. import vector_store

    total = len(file_ids)

    # ---- Phase 1: cancelling stale extracts ---------------------------------
    cancelled = 0
    with session_scope() as s:
        # One query per ~200 file_ids — SQLite has a default 999-parameter
        # limit, so chunked IN is the safest portable shape.
        for chunk in _chunked(file_ids, _REINDEX_PROGRESS_CHUNK):
            q = s.execute(
                select(Job).where(
                    Job.state == "queued",
                    Job.kind == "extract",
                    Job.dedup_key.in_([f"extract:{fid}" for fid in chunk]),
                )
            ).scalars()
            for j in q:
                j.state = "done"
                j.error = "superseded by reindex"
                j.finished_at = int(time.time())
                cancelled += 1
    if cancelled:
        logger.info("reindex: cancelled %d stale extract job(s)", cancelled)
    _publish_reindex_progress(folder_id, phase="cancelling", done=total, total=total)

    # ---- Phase 2: wiping (the slow phase, now bulk) -------------------------
    _publish_reindex_progress(folder_id, phase="wiping", done=0, total=total)

    # 2a. CAS decrefs need per-row context (each chunk + image references
    # one SHA), so we still touch every row — but in batches with one
    # transaction per batch, not one per file. The decref is a SQLite-only
    # bookkeeping update so it's cheap.
    with _EXTRACT_LOCK:
        wiped = 0
        for chunk in _chunked(file_ids, _REINDEX_PROGRESS_CHUNK):
            with session_scope() as s:
                # Decref CAS for every image whose file is being wiped.
                rows = s.execute(
                    select(Image.image_cas_id, Image.file_id).where(
                        Image.file_id.in_(chunk)
                    )
                ).all()
                for sha, _fid in rows:
                    cas_store.decref(s, cas_store.KIND_IMAGE, sha)
                # And for the file blob itself.
                file_shas = s.execute(
                    select(File.file_cas_id).where(
                        File.id.in_(chunk),
                        File.file_cas_id.is_not(None),
                    )
                ).all()
                for (sha,) in file_shas:
                    cas_store.decref(s, cas_store.KIND_FILE, sha)
                # Bulk row deletes. ChunkImageLink is CASCADEd from both
                # parents, so this is the only DELETE needed for it too.
                s.execute(sa_delete(Image).where(Image.file_id.in_(chunk)))
                s.execute(sa_delete(Chunk).where(Chunk.file_id.in_(chunk)))
            wiped += len(chunk)
            _publish_reindex_progress(
                folder_id, phase="wiping", done=wiped, total=total
            )

        # 2b. Wipe Qdrant chunk points for exactly the files being
        # re-extracted — scoped to ``file_ids``, NOT the whole folder. A
        # folder-wide delete here would nuke every other file's vectors on a
        # *partial* reindex (e.g. reindexing one file), leaving them indexed
        # in SQLite but absent from Qdrant. Batched filter-deletes keep a
        # full-folder reindex to a handful of round-trips.
        vector_store.delete_chunks_for_files(file_ids)
        deleted_image_points = vector_store.remove_files_from_image_points(file_ids)
        if deleted_image_points:
            logger.info(
                "reindex: removed %d image point(s) from Qdrant",
                deleted_image_points,
            )

    # ---- Phase 3: queueing fresh extracts -----------------------------------
    _publish_reindex_progress(folder_id, phase="queueing", done=0, total=total)
    upserts: list[int] = []
    queued = 0
    for chunk in _chunked(file_ids, _REINDEX_PROGRESS_CHUNK):
        with session_scope() as s:
            files = s.execute(select(File).where(File.id.in_(chunk))).scalars().all()
            for f in files:
                f.file_cas_id = None
                f.state = "pending"
                f.error = None
                f.pending_embeds = 0
                job_queue.enqueue(
                    s, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
                )
                upserts.append(f.id)
        queued += len(chunk)
        _publish_reindex_progress(
            folder_id, phase="queueing", done=queued, total=total
        )

    # Notify the SPA that every row flipped back to pending. Emit the
    # per-file events in ONE session and recompute folder stats just once
    # per touched folder.
    #
    # Do NOT call publish_file_upserted() in a per-file loop here: each call
    # recomputes full-folder stats (compute_folder_stats scans every file in
    # the folder + aggregate chunk/image queries + a Qdrant health probe), so
    # N files cost O(N^2). A 7k-file reindex wedged the single indexer worker
    # for many minutes spinning in compute_folder_stats (py-spy caught it
    # mid-loop), starving the queued extract jobs that share that one worker.
    from ..folder_stats import publish_folder_stats

    touched_folders: set[int] = set()
    with session_scope() as s:
        for fid in upserts:
            file = s.get(File, fid)
            if file is None:
                continue
            events.publish(
                "files",
                {"type": "file.upserted", "file": file_event_payload(file)},
            )
            touched_folders.add(file.folder_id)
        for touched in touched_folders:
            publish_folder_stats(s, touched)
    _publish_reindex_progress(folder_id, phase="done", done=total, total=total)
