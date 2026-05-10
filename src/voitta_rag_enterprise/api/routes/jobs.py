"""Job listing + retry endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import File, Job
from ...services import job_queue
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


def _to_out(j: Job, file_paths: dict[int, str] | None = None) -> JobOut:
    payload = json.loads(j.payload) if j.payload else {}
    display = None
    if file_paths is not None:
        fid = payload.get("file_id")
        if isinstance(fid, int):
            display = file_paths.get(fid)
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
    queued = db.execute(select(Job).where(Job.state == "queued")).scalars().all()
    for job in queued:
        job.state = "done"
        job.error = "cancelled"
        cancelled += 1

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
    return CancelAllOut(cancelled_queued=cancelled, killed_running=killed)
