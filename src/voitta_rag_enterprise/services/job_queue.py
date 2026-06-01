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
from . import events, folder_active

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
            # Dedup hit — no new row, no folder-active transition to publish.
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
    # New queued row — bump the folder-active counter. We resolve folder
    # in the caller's session so the lookup participates in their
    # transaction; if the caller rolls back, the in-memory count drifts
    # high until the next ``folder_active.init_from_db`` sweep. Rollback
    # of an enqueue is rare in this codebase (callers commit immediately)
    # so we accept the rare drift over the complexity of post-commit hooks.
    folder_active.on_enqueued(folder_active.folder_id_for_payload(session, payload))
    return job.id


def _find_inflight(session: Session, dedup_key: str) -> int | None:
    return session.execute(
        select(Job.id).where(
            Job.dedup_key == dedup_key,
            Job.state.in_(("queued", "running")),
        )
    ).scalar_one_or_none()


def reclaim_abandoned_jobs(*, max_attempts: int = 5) -> tuple[int, int]:
    """Mark every ``running`` row from a previous process as ``error``.

    A worker pool that dies mid-job (uvicorn --reload, OOM kill, ctrl-C,
    SIGKILL) leaves rows stuck at ``state='running'`` forever —
    ``claim_one`` only picks ``queued`` rows, and there is no other code
    path that resurrects them.

    Earlier this function requeued such rows so a fresh worker would
    retry, but that re-introduced the same problem on the next death:
    a parser that wedges once tends to wedge again, and the user ends
    up watching the queue stall on the same poison job across multiple
    restarts. The simpler rule — fail the job, move on, never retry —
    keeps the queue draining and surfaces the dead job in the UI so
    the operator can investigate manually.

    ``max_attempts`` is kept in the signature for backwards-compat but
    is unused; every ``running`` row is now moved to ``error``.

    Returns ``(requeued, killed)`` where ``requeued`` is always 0 and
    ``killed`` is the number of rows transitioned to ``error``.
    """
    _ = max_attempts  # kept for backwards-compat; no longer used
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
            s.execute(
                text(
                    "UPDATE jobs SET state='error', error=:e, "
                    "finished_at=:now WHERE id=:id"
                ),
                {
                    "id": jid,
                    "now": int(time.time()),
                    "e": (
                        f"abandoned in 'running' state on previous run "
                        f"(attempts={attempts}); marked error on startup "
                        "instead of retrying"
                    ),
                },
            )
            killed += 1
    return 0, killed


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
        # Resolve the owning folder so the WS layer can ACL-route this event
        # (drop it for connections whose user can't see the folder).
        folder_id = folder_active.folder_id_for_payload(session, claimed.payload)
    events.publish(
        "jobs",
        {
            "type": "job.started",
            "job_id": claimed.id,
            "kind": claimed.kind,
            "payload": claimed.payload,
            "display_path": display_path,
            "folder_id": folder_id,
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


def mark_done(job_id: int, result: dict | None = None) -> None:
    kind: str | None = None
    folder_id: int | None = None
    # Persist the handler's summary so it survives a reconnect (the snapshot
    # reads it back); guard against a non-serialisable return rather than
    # failing the whole job over a logging nicety.
    result_json: str | None = None
    if result is not None:
        try:
            result_json = json.dumps(result)
        except (TypeError, ValueError):
            logger.warning("job %d result not JSON-serialisable; dropping", job_id)
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        # Resolve folder *before* flipping state so a concurrent reader
        # that joins jobs↔files can still link them via state='running'.
        # Not strictly required (we hold the row) but keeps the lookup
        # symmetric with mark_error and the cancel path.
        try:
            payload = json.loads(job.payload) if job.payload else {}
        except json.JSONDecodeError:
            payload = {}
        folder_id = folder_active.folder_id_for_payload(session, payload)
        job.state = "done"
        job.finished_at = int(time.time())
        job.result = result_json
        kind = job.kind
    folder_active.on_finished(folder_id)
    events.publish(
        "jobs",
        {
            "type": "job.finished",
            "job_id": job_id,
            "kind": kind,
            "state": "done",
            "folder_id": folder_id,
            "result": result,
        },
    )


def mark_error(job_id: int, error: str) -> None:
    kind: str | None = None
    folder_id: int | None = None
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        try:
            payload = json.loads(job.payload) if job.payload else {}
        except json.JSONDecodeError:
            payload = {}
        folder_id = folder_active.folder_id_for_payload(session, payload)
        job.state = "error"
        job.error = error
        job.finished_at = int(time.time())
        kind = job.kind
    folder_active.on_finished(folder_id)
    events.publish(
        "jobs",
        {
            "type": "job.finished",
            "job_id": job_id,
            "kind": kind,
            "state": "error",
            "error": error,
            "folder_id": folder_id,
        },
    )
