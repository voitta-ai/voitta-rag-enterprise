"""Sync + indexing state across a viewer's folders — the assistant's view.

MCP exposes the *content* of the index; nothing exposes its *health* in a
form an LLM can reason over: which folders sync from where, when they last
synced, whether that failed, how much is indexed vs stuck, and what the
queue is doing. This module assembles that, scoped to what the viewer may
see:

* every visible folder contributes its status and file-state counts;
* the source configuration digest and the raw sync error text are shown
  only to the folder's owner — the same people the owner-only sync routes
  (``api/routes/sync``) would show them to.

Credentials never leave this module: the configuration digest is built
from an explicit ALLOWLIST of non-secret columns per source type, so a new
secret column added to ``folder_sync_sources`` is excluded by default.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import File, Folder, FolderSyncSource, Job
from . import folder_active
from .acl import is_folder_owner
from .folder_stats import IN_PROGRESS_STATES, compute_folder_stats
from .job_listing import job_payload
from .retrieval import Viewer

# Non-secret FolderSyncSource columns, per source type, that describe WHAT
# a folder syncs. Anything not listed here (tokens, secrets, refresh tokens,
# service-account JSON, certificates, emails used for auth) is never read.
_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "github": ("gh_repo", "gh_path", "gh_branches", "gh_all_branches", "gh_extended"),
    "google_drive": (
        "gd_folder_id",
        "gd_files_only",
        "gd_shared_with_me",
        "gd_use_builtin",
        "gd_credential_id",
    ),
    "google_drive_local": ("gdl_account", "gdl_path", "gdl_paths"),
    "sharepoint": ("ms_tenant_id", "ms_auth_method", "sp_selected_sites", "sp_all_sites"),
    "teams": ("ms_tenant_id", "ms_auth_method", "tm_user_mode", "tm_include_attended"),
    "nfs": ("nfs_subpath", "nfs_subpaths"),
    "local_link": ("ll_path", "ll_ignore"),
    "jira": (
        "jira_base_url",
        "jira_auth_method",
        "jira_selected_projects",
        "jira_all_projects",
        "jira_jql",
        "jira_updated_since",
    ),
    "confluence": (
        "cf_base_url",
        "cf_auth_method",
        "cf_selected_spaces",
        "cf_all_spaces",
        "cf_cql",
        "cf_updated_since",
    ),
}

# Columns stored as JSON text; decoded so the model sees structure.
_JSON_FIELDS = frozenset({
    "gh_branches", "gdl_paths", "sp_selected_sites", "nfs_subpaths",
    "ll_ignore", "jira_selected_projects", "cf_selected_spaces",
})

def _iso(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


class FileCounts(BaseModel):
    total: int = 0
    indexed: int = 0
    pending: int = 0
    in_progress: int = 0
    error: int = 0
    unsupported: int = 0


class FolderSyncSummary(BaseModel):
    folder_id: int
    display_name: str
    # "local" = files uploaded / written in place, no remote source.
    source_type: str
    owned: bool
    shared: bool
    # idle | queued | running | error — from the source row; "idle" for local.
    sync_status: str
    sync_in_flight: bool = False
    last_synced_at: str | None = None
    auto_sync_enabled: bool = False
    auto_sync_hours: int | None = None
    next_auto_sync_at: str | None = None
    # True while any extract/embed/sync job for this folder is queued/running.
    indexing_active: bool = False
    files: FileCounts = Field(default_factory=FileCounts)
    # Owner-only: the last sync error text (None for non-owners).
    sync_error: str | None = None


class SyncOverview(BaseModel):
    generated_at: str
    folders: list[FolderSyncSummary]


class LastSyncRun(BaseModel):
    job_id: int
    state: str
    finished_at: str | None = None
    error: str | None = None
    result: dict | None = None


class FolderSyncDetail(BaseModel):
    summary: FolderSyncSummary
    # Owner-only non-secret description of what the folder syncs from.
    source_config: dict[str, Any] | None = None
    # SQLite "indexed" vs Qdrant points — "out_of_sync" means search can't
    # see files the UI reports as indexed (needs a reindex).
    index_health: dict[str, Any] | None = None
    by_extension: dict[str, dict[str, int]] = Field(default_factory=dict)
    chunks_total: int = 0
    images_total: int = 0
    bytes_total: int = 0
    last_sync_runs: list[LastSyncRun] = Field(default_factory=list)


class FileProblem(BaseModel):
    file_id: int
    rel_path: str
    state: str
    error: str | None = None
    source_url: str | None = None


def _visible_folders(db: Session, viewer: Viewer) -> list[Folder]:
    allowed = viewer.visible_folder_ids(db)
    stmt = select(Folder).order_by(Folder.id)
    if allowed is not None:
        if not allowed:
            return []
        stmt = stmt.where(Folder.id.in_(allowed))
    return list(db.execute(stmt).scalars())


def _owns(db: Session, viewer: Viewer, folder_id: int) -> bool:
    return viewer.user_id is None or is_folder_owner(db, folder_id, viewer.user_id)


def _file_counts(db: Session, folder_ids: list[int]) -> dict[int, FileCounts]:
    out: dict[int, FileCounts] = defaultdict(FileCounts)
    if not folder_ids:
        return out
    rows = db.execute(
        select(File.folder_id, File.state, func.count(File.id))
        .where(File.folder_id.in_(folder_ids), File.state != "deleted")
        .group_by(File.folder_id, File.state)
    ).all()
    for folder_id, state, n in rows:
        c = out[folder_id]
        c.total += n
        if state == "indexed":
            c.indexed += n
        elif state == "error":
            c.error += n
        elif state == "unsupported":
            c.unsupported += n
        elif state in IN_PROGRESS_STATES:
            c.in_progress += n
        else:
            c.pending += n
    return out


def _sync_in_flight(db: Session) -> set[int]:
    """Folder ids with a queued/running sync job (dedup key ``sync:<id>``)."""
    keys = db.execute(
        select(Job.dedup_key).where(
            Job.dedup_key.like("sync:%"), Job.state.in_(("queued", "running"))
        )
    ).scalars()
    out: set[int] = set()
    for key in keys:
        suffix = (key or "").partition(":")[2]
        if suffix.isdigit():
            out.add(int(suffix))
    return out


def _summary(
    db: Session,
    viewer: Viewer,
    folder: Folder,
    src: FolderSyncSource | None,
    counts: FileCounts,
    in_flight: set[int],
) -> FolderSyncSummary:
    owned = _owns(db, viewer, folder.id)
    next_auto = None
    if src is not None and src.auto_sync_enabled:
        # Mirrors services/scheduler: due ``hours`` after the last sync, or
        # immediately (next tick) when it has never synced.
        hours = max(1, min(24, int(src.auto_sync_hours or 6)))
        due = (src.last_synced_at or 0) + hours * 3600
        next_auto = _iso(max(due, int(time.time())))
    return FolderSyncSummary(
        folder_id=folder.id,
        display_name=folder.display_name,
        source_type=src.source_type if src is not None else "local",
        owned=owned,
        shared=bool(folder.shared),
        sync_status=src.sync_status if src is not None else "idle",
        sync_in_flight=folder.id in in_flight,
        last_synced_at=_iso(src.last_synced_at) if src is not None else None,
        auto_sync_enabled=bool(src.auto_sync_enabled) if src is not None else False,
        auto_sync_hours=src.auto_sync_hours if src is not None else None,
        next_auto_sync_at=next_auto,
        indexing_active=folder_active.is_active(folder.id),
        files=counts,
        sync_error=(src.sync_error if (src is not None and owned) else None),
    )


def sync_overview(db: Session, viewer: Viewer) -> SyncOverview:
    """Status of every folder the viewer can see, in one pass."""
    folders = _visible_folders(db, viewer)
    ids = [f.id for f in folders]
    sources = {
        s.folder_id: s
        for s in db.execute(
            select(FolderSyncSource).where(FolderSyncSource.folder_id.in_(ids))
        ).scalars()
    } if ids else {}
    counts = _file_counts(db, ids)
    in_flight = _sync_in_flight(db)
    return SyncOverview(
        generated_at=_iso(int(time.time())) or "",
        folders=[
            _summary(db, viewer, f, sources.get(f.id), counts[f.id], in_flight)
            for f in folders
        ],
    )


def _source_config(src: FolderSyncSource) -> dict[str, Any]:
    out: dict[str, Any] = {"source_type": src.source_type}
    for name in _CONFIG_FIELDS.get(src.source_type, ()):
        value = getattr(src, name, None)
        if name in _JSON_FIELDS and isinstance(value, str) and value:
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
        out[name] = value
    return out


def _last_sync_runs(db: Session, folder_id: int, limit: int) -> list[LastSyncRun]:
    jobs = db.execute(
        select(Job)
        .where(Job.dedup_key == f"sync:{folder_id}", Job.state.in_(("done", "error")))
        .order_by(Job.id.desc())
        .limit(limit)
    ).scalars()
    runs: list[LastSyncRun] = []
    for j in jobs:
        try:
            result = json.loads(j.result) if j.result else None
        except json.JSONDecodeError:
            result = None
        runs.append(
            LastSyncRun(
                job_id=j.id,
                state=j.state,
                finished_at=_iso(j.finished_at),
                error=j.error,
                result=result if isinstance(result, dict) else None,
            )
        )
    return runs


def folder_sync_detail(
    db: Session, viewer: Viewer, folder_id: int, *, runs: int = 5
) -> FolderSyncDetail:
    """Deep view of one folder. Raises ``ValueError`` if not visible."""
    folder = db.get(Folder, folder_id)
    if folder is None or not viewer.can_see_folder(db, folder_id):
        raise ValueError(f"Folder {folder_id} not found")
    src = db.get(FolderSyncSource, folder_id)
    counts = _file_counts(db, [folder_id])[folder_id]
    summary = _summary(db, viewer, folder, src, counts, _sync_in_flight(db))
    stats = compute_folder_stats(db, folder, include_health=True)
    owned = summary.owned
    return FolderSyncDetail(
        summary=summary,
        source_config=_source_config(src) if (src is not None and owned) else None,
        index_health=stats.get("index_health"),
        by_extension=stats["by_extension"],
        chunks_total=stats["chunks_total"],
        images_total=stats["images_total"],
        bytes_total=stats["bytes_total"],
        last_sync_runs=_last_sync_runs(db, folder_id, runs) if owned else [],
    )


def file_problems(
    db: Session,
    viewer: Viewer,
    folder_id: int,
    *,
    states: tuple[str, ...] = ("error", "unsupported"),
    limit: int = 50,
) -> list[FileProblem]:
    """Files in a folder that did not index, with the recorded reason."""
    if not viewer.can_see_folder(db, folder_id) or db.get(Folder, folder_id) is None:
        raise ValueError(f"Folder {folder_id} not found")
    rows = db.execute(
        select(File)
        .where(File.folder_id == folder_id, File.state.in_(states))
        .order_by(File.rel_path)
        .limit(max(1, min(limit, 500)))
    ).scalars()
    return [
        FileProblem(
            file_id=f.id,
            rel_path=f.rel_path,
            state=f.state,
            error=f.error,
            source_url=f.source_url,
        )
        for f in rows
    ]


def recent_jobs_view(db: Session, viewer: Viewer, *, limit: int = 30) -> list[dict[str, Any]]:
    """Recent + running jobs on visible folders (same query as the Jobs panel)."""
    from .job_listing import recent_visible_jobs

    listing = recent_visible_jobs(db, viewer.visible_folder_ids(db), limit=limit)
    out: list[dict[str, Any]] = []
    for j in listing.jobs:
        payload = job_payload(j)
        file_id = payload.get("file_id")
        out.append(
            {
                "job_id": j.id,
                "kind": j.kind,
                "state": j.state,
                "folder_id": folder_active.folder_id_for_payload(db, payload),
                "file": listing.file_paths.get(file_id) if isinstance(file_id, int) else None,
                "attempts": j.attempts,
                "enqueued_at": _iso(j.enqueued_at),
                "started_at": _iso(j.started_at),
                "finished_at": _iso(j.finished_at),
                "error": j.error,
            }
        )
    return out
