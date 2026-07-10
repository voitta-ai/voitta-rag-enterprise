"""Startup recovery: reset files left mid-pipeline when their extract job died."""

from __future__ import annotations

import json

from sqlalchemy import select

from ...db.database import session_scope
from ...db.models import File, Image, Job
from .. import job_queue
from .common import logger, publish_file_upserted


def reconcile_abandoned_extracts() -> int:
    """Reset files left in mid-pipeline state when their extract job died.

    A worker that crashed during ``_run_extract_inner`` (uvicorn --reload,
    OOM, ctrl-C) leaves the file row at ``state='extracted'`` or
    ``state='embedding'`` with a previous round's ``pending_embeds`` value
    that no longer matches any in-flight job — and ``_commit_indexing``
    already ran (or never reached the embed enqueue), so neither the file
    nor the queue knows that work is unfinished.

    For every such row that has *no* live extract/embed job, we kick it
    back to ``state='pending'``: the watcher's previously-emitted event for
    this file is gone, but ``reclaim_abandoned_jobs`` ran first and may
    have requeued the original extract; failing that, the next folder
    scan will pick it up.

    Files where ``pending_embeds=0`` and a CAS row exists take a fast path
    below: snap straight to ``indexed`` instead of cycling through a
    no-op re-extract.

    We also re-enqueue *pending* files that have no live extract job.
    Without this, a cancel-all from a previous run leaves files sitting
    in ``state='pending'`` forever: their dedup'd extract is in 'done'
    state so a new one would be admitted, but nothing actually calls
    enqueue() because the scanner only re-enqueues on mtime/size change.
    """
    repaired_ids: list[int] = []
    with session_scope() as s:
        candidates = list(
            s.execute(
                select(File).where(
                    File.state.in_(("extracted", "embedding")),
                )
            ).scalars()
        )
        # Build set of file_ids referenced by any live (queued/running)
        # extract or embed job — those don't need our help.
        live_files: set[int] = set()
        rows = s.execute(
            select(Job).where(
                Job.state.in_(("queued", "running")),
                Job.kind.in_(("extract", "embed_text", "embed_image")),
            )
        ).scalars()
        for j in rows:
            try:
                payload = json.loads(j.payload)
            except (TypeError, ValueError):
                continue
            fid = payload.get("file_id")
            if isinstance(fid, int):
                live_files.add(fid)
            elif "image_id" in payload:
                row = s.execute(
                    select(Image.file_id).where(
                        Image.id == int(payload["image_id"])
                    )
                ).first()
                if row is not None:
                    live_files.add(row[0])

        for f in candidates:
            if f.id in live_files:
                continue
            # Fast path: pending_embeds=0 with a CAS row means the prior run
            # already wrote text/chunks/images and the corresponding Qdrant
            # points (the decrement just got lost on the way out). Snap to
            # indexed instead of cycling through pending + a no-op extract
            # that would short-circuit on unchanged sha and leave the file
            # stuck in pending.
            if f.pending_embeds == 0 and f.file_cas_id is not None:
                logger.warning(
                    "reconcile: file_id=%d was stuck (state=%s pending=0) "
                    "— forcing indexed",
                    f.id,
                    f.state,
                )
                f.state = "indexed"
                f.error = None
                repaired_ids.append(f.id)
                continue
            logger.warning(
                "reset abandoned extract: file_id=%d state=%s pending=%d",
                f.id,
                f.state,
                f.pending_embeds,
            )
            f.state = "pending"
            # pending_embeds is deliberately preserved: a positive count is
            # the signal _short_circuit_unchanged relies on to force a full
            # re-extract. Zeroing it here would let the re-enqueued extract
            # short-circuit on the unchanged sha and heal the row straight
            # to 'indexed' while its Qdrant points (lost with the embeds
            # this extract died in the middle of) are missing.
            f.error = None
            # Re-enqueue an extract job; reclaim_abandoned_jobs already ran
            # so this won't dedup against the dead one.
            job_queue.enqueue(
                s, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
            )
            repaired_ids.append(f.id)

        # Pending-orphan sweep: files left in 'pending' with no live extract
        # job. Happens when a cancel-all from a previous run dropped every
        # queued extract; the file row keeps its 'pending' state but no
        # code path re-enqueues it (scanner only re-enqueues on mtime/size
        # change). Without this, a hand-rolled `cancel-all` mid-sync leaves
        # hundreds of files in permanent limbo.
        stranded_pending = list(
            s.execute(select(File).where(File.state == "pending")).scalars()
        )
        already_repaired = set(repaired_ids)
        for f in stranded_pending:
            # Skip rows the candidate loop above just reset to 'pending' —
            # their extract is already enqueued; re-visiting them here only
            # double-counted them in the repaired total and published a
            # duplicate event.
            if f.id in live_files or f.id in already_repaired:
                continue
            logger.warning(
                "reconcile: re-enqueueing stranded pending file_id=%d", f.id
            )
            job_queue.enqueue(
                s, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
            )
            repaired_ids.append(f.id)
    for fid in repaired_ids:
        publish_file_upserted(fid)
    return len(repaired_ids)
