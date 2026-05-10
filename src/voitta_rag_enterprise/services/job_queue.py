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


def reclaim_abandoned_jobs(*, max_attempts: int = 5) -> tuple[int, int]:
    """Reset jobs left in ``running`` from a previous process.

    A worker pool that dies mid-job (uvicorn --reload, OOM kill, ctrl-C)
    leaves rows stuck at ``state='running'`` forever — ``claim_one`` only
    picks ``queued`` rows, and there is no other code path that resurrects
    them.

    On startup we scan every ``running`` row:
    * if its attempts count is below ``max_attempts``, push it back to
      ``queued`` so a fresh worker can re-run it;
    * if it has hit the cap, mark it ``error`` with a synthetic message so
      the UI surfaces the dead job instead of hiding it.

    Returns ``(requeued, killed)`` so the caller can log what happened.
    """
    requeued = 0
    killed = 0
    with session_scope() as s:
        rows = list(
            s.execute(
                text(
                    "SELECT id, attempts FROM jobs WHERE state='running'"
                )
            )
        )
        for jid, attempts in rows:
            if attempts >= max_attempts:
                s.execute(
                    text(
                        "UPDATE jobs SET state='error', error=:e, "
                        "finished_at=:now WHERE id=:id"
                    ),
                    {
                        "id": jid,
                        "now": int(time.time()),
                        "e": (
                            f"abandoned in 'running' state after {attempts} "
                            "attempts (worker pool died mid-job)"
                        ),
                    },
                )
                killed += 1
            else:
                s.execute(
                    text(
                        "UPDATE jobs SET state='queued', started_at=NULL "
                        "WHERE id=:id"
                    ),
                    {"id": jid},
                )
                requeued += 1
    return requeued, killed


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
        # Resolve display_path inside the same session so the SPA can
        # render ``extract #2912 — Lucid/big.json`` when the job.started
        # event lands. Without this the SPA only knows the file_id and
        # would have to round-trip back to /api/files for every claim.
        display_path = _resolve_display_path(session, claimed.payload)
    events.publish(
        "jobs",
        {
            "type": "job.started",
            "job_id": claimed.id,
            "kind": claimed.kind,
            "payload": claimed.payload,
            "display_path": display_path,
        },
    )
    return claimed


def _resolve_display_path(session, payload: dict) -> str | None:
    """Look up ``files.rel_path`` for a job payload's ``file_id`` field.

    Used to enrich ``job.started`` events with a human-readable target
    so the Jobs panel doesn't reduce every running job to a bare
    ``extract #2912``. Returns None for jobs whose payload doesn't
    reference a file (sync, reindex_folder, etc.) — those are
    surfaced under their folder context elsewhere.
    """
    fid = payload.get("file_id") if isinstance(payload, dict) else None
    if not isinstance(fid, int):
        return None
    return session.execute(
        text("SELECT rel_path FROM files WHERE id = :id"), {"id": fid}
    ).scalar()


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
