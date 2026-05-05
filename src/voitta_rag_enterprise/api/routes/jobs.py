"""Job listing + retry endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Job
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


def _to_out(j: Job) -> JobOut:
    return JobOut(
        id=j.id,
        kind=j.kind,
        state=j.state,
        payload=json.loads(j.payload) if j.payload else {},
        attempts=j.attempts,
        enqueued_at=j.enqueued_at,
        started_at=j.started_at,
        finished_at=j.finished_at,
        error=j.error,
        dedup_key=j.dedup_key,
    )


@router.get("/recent", response_model=list[JobOut])
def recent_jobs(
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[JobOut]:
    rows = (
        db.execute(select(Job).order_by(Job.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [_to_out(j) for j in rows]


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
