"""Job listing + retry endpoints."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import File, Job
from ...services import folder_active, job_queue
from ...services.acl import CurrentUser
from ..deps import current_user, db_session

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: int
    kind: str
    state: str
    payload: dict
    attempts: int
    enqueued_at: int
    started_at: int | None
    finished_at: int | None
    error: str | None
    dedup_key: str | None
    # Pre-resolved human label for the job's target. For ``extract`` /
    # ``embed_text`` / ``embed_image`` / ``delete_file`` jobs whose
    # payload references a file, this is the file's ``rel_path`` so
    # the SPA can render ``extract #2912 — Lucid Drive/big.json``
    # without a per-job round-trip back to the file API.
    display_path: str | None = None
    # Handler summary (sync stats, etc.) for the Jobs panel's expandable
    # detail; None for jobs that reported nothing.
    result: dict | None = None


def _to_out(j: Job, file_paths: dict[int, str] | None = None) -> JobOut:
    payload = json.loads(j.payload) if j.payload else {}
    display = None
    if file_paths is not None:
        fid = payload.get("file_id")
        if isinstance(fid, int):
            display = file_paths.get(fid)
    try:
        result = json.loads(j.result) if j.result else None
    except (json.JSONDecodeError, TypeError):
        result = None
    return JobOut(
        id=j.id,
        kind=j.kind,
        state=j.state,
        payload=payload,
        attempts=j.attempts,
        enqueued_at=j.enqueued_at,
        started_at=j.started_at,
        finished_at=j.finished_at,
        error=j.error,
        dedup_key=j.dedup_key,
        display_path=display,
        result=result,
    )


@router.get("/recent", response_model=list[JobOut])
def recent_jobs(
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[JobOut]:
    """Return the recent jobs for the SPA's Jobs panel.

    Composition: every ``running`` job plus the most recent ``limit``
    jobs by id. The union ensures the row that's actually consuming the
    worker is always visible — without this, a queue of 800 fresh
    extracts would push the running job (whose id is older) off the
    bottom of the panel and the SPA shows nothing as "running" even
    though something definitely is.

    De-duplication: if a running job already lands in the most-recent
    window we don't emit it twice. Order: running first (so the
    bottleneck is at the top), then queued/done by id desc within each
    bucket — what the user actually wants to see when scanning.
    """
    running = (
        db.execute(
            select(Job).where(Job.state == "running").order_by(Job.id.desc())
        )
        .scalars()
        .all()
    )
    recent = (
        db.execute(select(Job).order_by(Job.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    seen: set[int] = set()
    ordered: list[Job] = []
    for j in [*running, *recent]:
        if j.id in seen:
            continue
        seen.add(j.id)
        ordered.append(j)

    # Resolve file_id → rel_path in one query for everything we're
    # about to ship — beats N round-trips when the user has 30+ rows.
    file_ids: set[int] = set()
    for j in ordered:
        try:
            payload = json.loads(j.payload) if j.payload else {}
        except json.JSONDecodeError:
            continue
        fid = payload.get("file_id")
        if isinstance(fid, int):
            file_ids.add(fid)
    file_paths: dict[int, str] = {}
    if file_ids:
        rows = db.execute(
            select(File.id, File.rel_path).where(File.id.in_(file_ids))
        ).all()
        file_paths = dict(rows)

    return [_to_out(j, file_paths) for j in ordered]


class RetryOut(BaseModel):
    new_job_id: int


@router.post("/{job_id}/retry", response_model=RetryOut)
def retry_job(
    job_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> RetryOut:
    """Re-enqueue a failed job. The original error row is preserved for audit."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.state != "error":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only failed jobs can be retried (current state: {job.state})",
        )
    payload = json.loads(job.payload) if job.payload else {}
    new_id = job_queue.enqueue(
        db, job.kind, payload, dedup_key=job.dedup_key, priority=job.priority
    )
    db.commit()
    return RetryOut(new_job_id=new_id)


class RetryAllOut(BaseModel):
    retried: int
    skipped: int


@router.post("/retry-failed", response_model=RetryAllOut)
def retry_failed(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> RetryAllOut:
    """Re-enqueue every job currently in ``error`` state."""
    failed = db.execute(select(Job).where(Job.state == "error").order_by(Job.id)).scalars().all()
    retried = 0
    skipped = 0
    for job in failed:
        payload = json.loads(job.payload) if job.payload else {}
        new_id = job_queue.enqueue(
            db, job.kind, payload, dedup_key=job.dedup_key, priority=job.priority
        )
        if new_id == job.id:
            skipped += 1  # collapsed onto an already-queued retry
        else:
            retried += 1
    db.commit()
    return RetryAllOut(retried=retried, skipped=skipped)


@router.delete("/cleanup-failed", response_model=RetryAllOut)
def cleanup_failed(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> RetryAllOut:
    """Delete every ``error``-state job row. Useful after a successful retry sweep."""
    failed = db.execute(select(Job).where(Job.state == "error")).scalars().all()
    removed = 0
    for job in failed:
        db.delete(job)
        removed += 1
    db.commit()
    return RetryAllOut(retried=removed, skipped=0)


class CancelAllOut(BaseModel):
    cancelled_queued: int
    killed_running: int


@router.post("/cancel-all", response_model=CancelAllOut)
def cancel_all(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> CancelAllOut:
    """Drain every queued job and kill the currently-running one (if any).

    Three things happen, in order:

    1. ``state='queued'`` rows are flipped to ``state='done'`` with
       ``error='cancelled'``. They will never run.
    2. The MinerU subprocess (if alive) is SIGKILLed. The parent's
       ``readline`` returns empty, the parse raises TimeoutError, and
       the extract handler routes the file to ``state='error'`` —
       which is fine: the user can retry or reindex it.
    3. ``state='running'`` rows whose handler doesn't yet hold the
       MinerU subprocess (embed_text / embed_image runs on the GPU,
       sync runs in pure Python) are left alone — we have no
       interrupt for them. They finish naturally; the queue is empty
       behind them so the worker comes to a halt afterwards.

    No-op on a quiet queue. Race with a queued→running transition:
    benign, the now-running job either started before our UPDATE (it
    will finish normally) or after (it sees its own row already in
    ``done`` and skips).
    """
    from ...services.parsers import pdf_parser

    cancelled = 0
    cancelled_file_ids: list[int] = []
    # Collect folder_ids so we can drop their active counts after commit.
    # Resolving *before* the state flip keeps the lookup deterministic
    # against a parallel claim_one (which would only narrow the result).
    affected_folders: list[int | None] = []
    queued = db.execute(select(Job).where(Job.state == "queued")).scalars().all()
    for job in queued:
        try:
            payload = json.loads(job.payload) if job.payload else {}
        except json.JSONDecodeError:
            payload = {}
        affected_folders.append(folder_active.folder_id_for_payload(db, payload))
        job.state = "done"
        job.error = "cancelled"
        cancelled += 1
        fid = _mark_file_cancelled_from_queued(db, job)
        if fid is not None:
            cancelled_file_ids.append(fid)

    killed = 0
    # Kill any live MinerU subprocess; this unblocks an in-flight
    # extract by making its readline() return empty. If the daemon
    # isn't alive (no extract has run yet, or it already exited),
    # this is a no-op.
    if pdf_parser._DAEMON is not None and pdf_parser._DAEMON._proc is not None:
        if pdf_parser._DAEMON._proc.poll() is None:
            pdf_parser._DAEMON._kill("user cancel")
            killed = 1

    db.commit()

    # Drop the active counts for every cancelled job *after* commit so a
    # SPA reading active=False can trust that the underlying row is no
    # longer 'queued'. Events coalesce by folder_id so N drops on the
    # same folder collapse to one delivered event.
    for fid in affected_folders:
        folder_active.on_finished(fid)

    # Publish file.upserted for every file whose state we just flipped to
    # 'error', so the SPA's by-extension counters reflect the cancel
    # without waiting for a refresh. Without this, files stayed in
    # 'pending' on the UI even though their job had been killed.
    if cancelled_file_ids:
        from ...services.indexing import publish_file_upserted

        for fid in cancelled_file_ids:
            publish_file_upserted(fid)

    return CancelAllOut(cancelled_queued=cancelled, killed_running=killed)


# File-touching job kinds: cancelling these from the 'queued' state must
# also flip the file row to 'error' so the SPA doesn't show a phantom
# 'pending' file with no live job. delete_file is intentionally excluded
# (the file is already being torn down; resurrecting it as 'error' would
# be confusing).
_FILE_JOB_KINDS = {"extract", "embed_text", "embed_image"}


def _mark_file_cancelled_from_queued(db: Session, job: Job) -> int | None:
    """Mirror the running-branch file stamping for a cancelled queued job.

    Without this, cancelling a queued extract leaves the file row in
    ``state='pending'`` with no active job pointing at it — orphaned
    forever because the scanner only re-enqueues on mtime/size change.

    Returns the affected file_id (for SPA publishing), or None if this
    job has no associated file row.
    """
    if job.kind not in _FILE_JOB_KINDS:
        return None
    try:
        payload = json.loads(job.payload) if job.payload else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    fid = payload.get("file_id")
    if not isinstance(fid, int):
        return None
    f = db.get(File, fid)
    if f is None:
        return None
    # Match the running-branch treatment in cancel_job: bump embed_round so
    # a late embed decrement can't flip the row back to 'indexed' on us;
    # zero pending_embeds so reconcile won't pick it back up; stamp the
    # error message so the SPA shows a clear retryable state.
    f.embed_round = (f.embed_round or 0) + 1
    f.state = "error"
    f.error = "cancelled by user"
    f.pending_embeds = 0
    return fid


class CancelOneOut(BaseModel):
    job_id: int
    state: str
    note: str | None = None  # human-readable side effect summary


@router.post("/{job_id}/cancel", response_model=CancelOneOut)
def cancel_job(
    job_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> CancelOneOut:
    """Cancel a single queued or running job.

    Behaviour by current state:

    * ``queued`` — flip to ``state='done'``, ``error='cancelled'``.
      The job will never run.
    * ``running`` — same flip, plus best-effort interrupt:
        - ``extract`` PDF runs: kill the MinerU subprocess so the
          worker thread's ``readline`` returns empty and the parse
          aborts; file lands in ``state='error'``.
        - ``extract`` non-PDF / ``embed_*`` / ``sync``: bump the
          file's ``embed_round`` (when applicable) so the in-flight
          embed's ``_decrement_pending_embeds`` no-ops on completion,
          and mark the file as ``error`` with ``cancelled by user`` so
          the SPA stops showing it as in-progress. The worker thread
          keeps running silently until its current sequential pass
          finishes — Python has no clean interrupt for an embedder
          forward pass that's already entered native code. The
          ``note`` field surfaces this caveat to the SPA.
    * ``done`` / ``error`` — 400, already terminal.

    No-op race with worker pool draining the same row is fine: the
    UPDATE just lands on a ``done`` row.
    """
    from ...services.parsers import pdf_parser

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.state not in ("queued", "running"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot cancel job in state {job.state!r} (only queued / running)",
        )

    was_running = job.state == "running"
    job_kind = job.kind
    # Resolve folder *before* flipping state — the running-branch below
    # reads/mutates the file row off the same payload and we want the
    # active-tracker decrement to use the same folder_id the file lookup
    # will resolve to.
    try:
        cancel_payload = json.loads(job.payload) if job.payload else {}
    except json.JSONDecodeError:
        cancel_payload = {}
    cancel_folder_id = folder_active.folder_id_for_payload(db, cancel_payload)
    job.state = "done"
    job.error = "cancelled"
    job.finished_at = int(time.time())

    note: str | None = None
    file_to_publish: int | None = None
    if not was_running:
        # Queued branch: stamp the file row so it doesn't become an orphan.
        # The running branch has its own, richer treatment below.
        file_to_publish = _mark_file_cancelled_from_queued(db, job)
    if was_running:
        try:
            payload = json.loads(job.payload) if job.payload else {}
        except json.JSONDecodeError:
            payload = {}

        # PDF parse path: a SIGKILL on the MinerU daemon unblocks the
        # extract handler immediately. Everything else only gets a
        # state-flip — surface that asymmetry so the SPA can warn.
        killed_pdf = False
        if (
            job_kind == "extract"
            and pdf_parser._DAEMON is not None
            and pdf_parser._DAEMON._proc is not None
            and pdf_parser._DAEMON._proc.poll() is None
        ):
            pdf_parser._DAEMON._kill("user cancel")
            killed_pdf = True

        # Bump the file's ``embed_round`` so a stale decrement landing
        # after this cancel doesn't flip the row to ``indexed``. Same
        # mechanism the reindex pipeline uses (see _commit_indexing).
        # Stamp the file as ``error`` so the file row's UI state matches
        # the job's. ``pending_embeds=0`` keeps reconcile from picking
        # the row back up later.
        fid = payload.get("file_id") if isinstance(payload, dict) else None
        if isinstance(fid, int):
            f = db.get(File, fid)
            if f is not None:
                f.embed_round = (f.embed_round or 0) + 1
                f.state = "error"
                f.error = "cancelled by user"
                f.pending_embeds = 0
                file_to_publish = fid

        if killed_pdf:
            note = "MinerU subprocess killed; file marked as error."
        elif file_to_publish is not None or job_kind in ("embed_text", "embed_image"):
            note = (
                "Job marked cancelled. The worker thread is still running "
                "the current pass and will finish silently in the "
                "background; the file is already marked as error so it "
                "won't show as in-progress."
            )

    db.commit()

    # Drop this folder's active count *after* commit so a SPA that races
    # the active=False event back to the DB will see the cancelled row.
    folder_active.on_finished(cancel_folder_id)

    # Publish job.finished + file.upserted so the SPA flips both rows
    # immediately, without waiting for the next /recent or /files poll.
    from ...services import events

    events.publish(
        "jobs",
        {
            "type": "job.finished",
            "job_id": job_id,
            "kind": job_kind,
            "state": "done",
            "error": "cancelled",
        },
    )
    if file_to_publish is not None:
        from ...services.indexing import publish_file_upserted

        publish_file_upserted(file_to_publish)

    return CancelOneOut(job_id=job_id, state="done", note=note)
