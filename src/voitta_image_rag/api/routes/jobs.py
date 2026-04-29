"""Job listing endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Job
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
