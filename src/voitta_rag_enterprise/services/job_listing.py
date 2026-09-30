"""The "recent jobs" view, folder-ACL scoped — one query for every consumer.

Three surfaces show the same list: ``GET /api/jobs/recent`` (Jobs panel
REST fallback), the WebSocket ``jobs`` snapshot, and the assistant's
``recent_jobs`` tool. They used to carry two copies of the query, and only
the WS copy filtered by folder visibility — the REST route listed every
tenant's jobs. Keeping one implementation here is what keeps them honest.

Composition: every ``running`` job plus the most recent ``limit`` jobs by
id, de-duplicated, running first. The union keeps the job that is actually
occupying the worker visible even when a burst of fresh enqueues would push
its (older) id out of the most-recent window.

Visibility: a job is shown when the folder its payload targets is in
``visible``. Jobs whose payload resolves to no folder (``gc_cas`` and other
deployment-wide housekeeping) carry no tenant data and are always shown.
``visible=None`` means see-everything (single-user mode only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import File, Job
from .folder_active import folder_id_for_payload


def job_payload(job: Job) -> dict[str, Any]:
    """The job's decoded payload, ``{}`` for an empty or corrupt row."""
    if not job.payload:
        return {}
    try:
        payload = json.loads(job.payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class JobListing:
    """Ordered jobs plus the ``file_id → rel_path`` labels they reference."""

    jobs: list[Job]
    file_paths: dict[int, str] = field(default_factory=dict)


def recent_visible_jobs(
    db: Session, visible: set[int] | None, *, limit: int = 50
) -> JobListing:
    """Running + ``limit`` most recent jobs the viewer may see (see module doc)."""
    running = db.execute(
        select(Job).where(Job.state == "running").order_by(Job.id.desc())
    ).scalars().all()
    recent = db.execute(
        select(Job).order_by(Job.id.desc()).limit(limit)
    ).scalars().all()

    seen: set[int] = set()
    ordered: list[Job] = []
    file_ids: set[int] = set()
    for job in [*running, *recent]:
        if job.id in seen:
            continue
        seen.add(job.id)
        payload = job_payload(job)
        if visible is not None:
            folder_id = folder_id_for_payload(db, payload)
            if folder_id is not None and folder_id not in visible:
                continue
        ordered.append(job)
        file_id = payload.get("file_id")
        if isinstance(file_id, int):
            file_ids.add(file_id)

    file_paths: dict[int, str] = {}
    if file_ids:
        file_paths = {
            fid: rel
            for fid, rel in db.execute(
                select(File.id, File.rel_path).where(File.id.in_(file_ids))
            ).all()
        }
    return JobListing(jobs=ordered, file_paths=file_paths)
