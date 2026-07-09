"""Build the full per-connection state snapshot sent over the WebSocket.

When a client connects (or reconnects), the WS handler sends a *snapshot* —
the complete current state for each subscribed topic, scoped to what the
connection's user is allowed to see — before streaming deltas. This is what
makes the WS the single source of truth: a reconnect re-snapshots, so a client
that missed events while offline converges back to server truth with no page
reload and no HTTP fallback.

The snapshot reuses the exact serializers the REST endpoints and WS deltas
already use (``_to_folder_out``, ``file_event_payload``, jobs ``_to_out``) so
the client applies snapshot items and subsequent deltas through one code path.

Each frame is ``{"type": "snapshot", "topic": <topic>, "items": [...]}``; the
handler emits one per subscribed topic, then a final ``{"type": "synced"}``.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import File, Folder, FolderSyncSource, Image, Job
from ..services import folder_active
from ..services.acl import folder_active_for_user, is_folder_owner
from ..services.indexing import file_event_payload


def _folders_snapshot(
    db: Session, user_id: int, visible: set[int] | None
) -> list[dict[str, Any]]:
    """FolderOut payloads for every visible folder (mirrors ``list_folders``)."""
    from .routes.folders import _sync_source_kind, _to_folder_out

    rows = db.execute(select(Folder).order_by(Folder.id)).scalars().all()
    sync_by_folder = {
        s.folder_id: s
        for s in db.execute(select(FolderSyncSource)).scalars().all()
    }
    see_all = visible is None
    out: list[dict[str, Any]] = []
    for f in rows:
        if not see_all and f.id not in visible:
            continue
        src = sync_by_folder.get(f.id)
        out.append(
            _to_folder_out(
                f,
                has_sync_source=src is not None,
                sync_source_kind=_sync_source_kind(src),
                sync_status=(src.sync_status if src else "idle"),
                owned=see_all or is_folder_owner(db, f.id, user_id),
                active=folder_active_for_user(db, f.id, user_id),
            ).model_dump()
        )
    return out


def _files_snapshot(
    db: Session, visible: set[int] | None
) -> list[dict[str, Any]]:
    """``file_event_payload`` for every file in a visible folder."""
    stmt = select(File).order_by(File.id)
    if visible is not None:
        if not visible:
            return []
        stmt = stmt.where(File.folder_id.in_(visible))
    files = db.execute(stmt).scalars().all()
    # One grouped count for the whole snapshot, not one query per file (the
    # payload carries image_count so the tree can gate expandability up front).
    counts = dict(
        db.execute(
            select(Image.file_id, func.count())
            .where(Image.file_id.in_([f.id for f in files]))
            .group_by(Image.file_id)
        ).all()
    ) if files else {}
    return [file_event_payload(f, image_count=counts.get(f.id, 0)) for f in files]


def _jobs_snapshot(
    db: Session, visible: set[int] | None, limit: int = 50
) -> list[dict[str, Any]]:
    """Recent + running jobs (mirrors ``recent_jobs``), ACL-scoped by folder."""
    from .routes.jobs import _to_out

    running = (
        db.execute(select(Job).where(Job.state == "running").order_by(Job.id.desc()))
        .scalars()
        .all()
    )
    recent = (
        db.execute(select(Job).order_by(Job.id.desc()).limit(limit)).scalars().all()
    )
    seen: set[int] = set()
    ordered: list[Job] = []
    for j in [*running, *recent]:
        if j.id in seen:
            continue
        seen.add(j.id)
        # ACL filter: drop jobs whose folder the user can't see. Admin /
        # single-user (visible is None) see everything.
        if visible is not None:
            try:
                payload = json.loads(j.payload) if j.payload else {}
            except json.JSONDecodeError:
                payload = {}
            fid = folder_active.folder_id_for_payload(db, payload)
            # Jobs with no resolvable folder (e.g. gc_cas) are global — keep.
            if fid is not None and fid not in visible:
                continue
        ordered.append(j)

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
        file_paths = dict(
            db.execute(
                select(File.id, File.rel_path).where(File.id.in_(file_ids))
            ).all()
        )
    return [_to_out(j, file_paths).model_dump() for j in ordered]


def build_snapshot(
    db: Session,
    *,
    user_id: int,
    is_admin: bool,
    visible: set[int] | None,
    topics: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Build the ordered list of snapshot frames for ``topics``.

    ``visible is None`` means see-everything (single-user mode only). Admins get
    a real ``visible`` set just like any other user — folder/file/job visibility
    is never widened by ``is_admin``; only the ``admin`` topic is. The active
    folder-id set is shipped as its own ``active`` snapshot frame (only when the
    ``folders`` topic is subscribed) since the SPA tracks it as a separate set.

    The ``admin`` frame is built only for admins; ``keys`` is always the
    connecting user's own keys. Both reuse the route-layer builders so a
    snapshot and an on-mutation push carry an identical shape.
    """
    frames: list[dict[str, Any]] = []
    if "admin" in topics and is_admin:
        from .routes.admin import build_admin_state

        frames.append({"type": "admin.snapshot", "state": build_admin_state(db)})
    if "keys" in topics and user_id:
        from .routes.api_keys import build_keys_state

        frames.append(
            {
                "type": "keys.snapshot",
                "user_id": user_id,
                "items": build_keys_state(db, user_id),
            }
        )
    if "folders" in topics:
        frames.append(
            {
                "type": "snapshot",
                "topic": "folders",
                "items": _folders_snapshot(db, user_id, visible),
            }
        )
        active = folder_active.get_active_ids()
        if visible is not None:
            active = [fid for fid in active if fid in visible]
        frames.append({"type": "snapshot", "topic": "active", "items": active})
    if "files" in topics:
        frames.append(
            {
                "type": "snapshot",
                "topic": "files",
                "items": _files_snapshot(db, visible),
            }
        )
    if "jobs" in topics:
        frames.append(
            {
                "type": "snapshot",
                "topic": "jobs",
                "items": _jobs_snapshot(db, visible),
            }
        )
    return frames
