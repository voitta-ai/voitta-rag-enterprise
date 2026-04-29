"""SQLite-backed job queue with per-key in-flight deduplication."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.database import session_scope
from ..db.models import Job
from . import events

logger = logging.getLogger(__name__)


@dataclass
class ClaimedJob:
    id: int
    kind: str
    payload: dict
    dedup_key: str | None
    attempts: int


def enqueue(
    session: Session,
    kind: str,
    payload: dict,
    *,
    dedup_key: str | None = None,
    priority: int = 0,
) -> int:
    """Insert a job; coalesce when ``dedup_key`` already has a queued/running job.

    Returns the job id that the caller can refer to (either the new one or the
    existing in-flight job's id).
    """
    payload_json = json.dumps(payload)
    if dedup_key:
        existing = _find_inflight(session, dedup_key)
        if existing is not None:
            return existing
    job = Job(
        kind=kind,
        payload=payload_json,
        dedup_key=dedup_key,
        priority=priority,
        state="queued",
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        if dedup_key:
            existing = _find_inflight(session, dedup_key)
            if existing is not None:
                return existing
        raise
    return job.id


def _find_inflight(session: Session, dedup_key: str) -> int | None:
    return session.execute(
        select(Job.id).where(
            Job.dedup_key == dedup_key,
            Job.state.in_(("queued", "running")),
        )
    ).scalar_one_or_none()


def claim_one() -> ClaimedJob | None:
    """Atomically claim the highest-priority queued job (if any)."""
    with session_scope() as session:
        row = session.execute(
            text(
                """
                UPDATE jobs
                SET state='running', started_at=:now, attempts=attempts+1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE state='queued'
                    ORDER BY priority DESC, id
                    LIMIT 1
                )
                RETURNING id, kind, payload, dedup_key, attempts
                """
            ),
            {"now": int(time.time())},
        ).first()
    if row is None:
        return None
    claimed = ClaimedJob(
        id=row[0], kind=row[1], payload=json.loads(row[2]), dedup_key=row[3], attempts=row[4]
    )
    events.publish(
        "jobs",
        {
            "type": "job.started",
            "job_id": claimed.id,
            "kind": claimed.kind,
            "payload": claimed.payload,
        },
    )
    return claimed


def mark_done(job_id: int) -> None:
    kind: str | None = None
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.state = "done"
        job.finished_at = int(time.time())
        kind = job.kind
    events.publish("jobs", {"type": "job.finished", "job_id": job_id, "kind": kind, "state": "done"})


def mark_error(job_id: int, error: str) -> None:
    kind: str | None = None
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.state = "error"
        job.error = error
        job.finished_at = int(time.time())
        kind = job.kind
    events.publish(
        "jobs",
        {
            "type": "job.finished",
            "job_id": job_id,
            "kind": kind,
            "state": "error",
            "error": error,
        },
    )
