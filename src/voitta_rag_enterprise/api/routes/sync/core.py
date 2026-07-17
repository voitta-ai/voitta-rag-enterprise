"""Source-agnostic sync endpoints: the CRUD envelope + trigger.

Everything per-source (validation, column mapping, response building,
pre-trigger readiness checks) is dispatched through ``registry`` — this
module has no knowledge of any specific connector beyond importing the
per-source schema classes for the envelope's typed fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....db.models import FolderSyncSource
from ....services import events, job_queue
from ....services.acl import CurrentUser
from ...deps import current_user, db_session
from . import registry
from .base import (
    SyncTriggerOut,
    check_access,
    check_owner,
    folder_has_real_files,
    publish_folder_changed,
    publish_sync_config_changed,
    router,
)
from .confluence import ConfluenceSyncIn, ConfluenceSyncOut
from .github import GithubSyncIn, GithubSyncOut
from .google_drive import GoogleDriveSyncIn, GoogleDriveSyncOut
from .google_local import GoogleDriveLocalSyncOut
from .jira import JiraSyncIn, JiraSyncOut
from .microsoft import SharePointSyncIn, SharePointSyncOut, TeamsSyncIn, TeamsSyncOut
from .nfs import NfsSyncIn, NfsSyncOut


class SyncSourceIn(BaseModel):
    source_type: Literal[
        "github", "google_drive", "nfs", "sharepoint", "teams", "jira",
        "confluence",
    ]
    github: GithubSyncIn | None = None
    google_drive: GoogleDriveSyncIn | None = None
    nfs: NfsSyncIn | None = None
    sharepoint: SharePointSyncIn | None = None
    teams: TeamsSyncIn | None = None
    jira: JiraSyncIn | None = None
    confluence: ConfluenceSyncIn | None = None
    # Periodic auto-sync. ``auto_sync_hours`` is bounded to 1-24 by the
    # scheduler — wider intervals belong to a real cron, not this in-process
    # loop. Both fields are common to all source types so they live on the
    # outer envelope, not inside the per-source blocks.
    auto_sync_enabled: bool = False
    auto_sync_hours: int = Field(default=6, ge=1, le=24)


class SyncSourceOut(BaseModel):
    folder_id: int
    source_type: str
    sync_status: str
    sync_error: str | None
    last_synced_at: int | None
    auto_sync_enabled: bool
    auto_sync_hours: int
    github: GithubSyncOut | None = None
    google_drive: GoogleDriveSyncOut | None = None
    google_drive_local: GoogleDriveLocalSyncOut | None = None
    nfs: NfsSyncOut | None = None
    sharepoint: SharePointSyncOut | None = None
    teams: TeamsSyncOut | None = None
    jira: JiraSyncOut | None = None
    confluence: ConfluenceSyncOut | None = None


def to_out(src: FolderSyncSource) -> SyncSourceOut:
    """Serialize a row: common fields + the active source's block, built by
    its registered handler. Secrets never appear — each ``build_out``
    reduces them to ``has_*`` booleans."""
    kwargs: dict = {
        "folder_id": src.folder_id,
        "source_type": src.source_type,
        "sync_status": src.sync_status,
        "sync_error": src.sync_error,
        "last_synced_at": src.last_synced_at,
        "auto_sync_enabled": bool(src.auto_sync_enabled),
        "auto_sync_hours": int(src.auto_sync_hours),
    }
    handler = registry.HANDLERS.get(src.source_type)
    if handler is not None and handler.build_out is not None:
        kwargs[handler.out_field] = handler.build_out(src)
    return SyncSourceOut(**kwargs)


# Package-internal alias kept for callers/tests that grew up with the old
# monolithic module's name.
_to_out = to_out


@router.get("", response_model=SyncSourceOut | None)
def get_sync_source(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut | None:
    check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    return to_out(src) if src is not None else None


@router.put("", response_model=SyncSourceOut)
def upsert_sync_source(
    folder_id: int,
    body: SyncSourceIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut:
    """Create or update a sync source.

    First-time configuration requires the folder to be empty (no indexed files,
    no on-disk files other than sync sidecars). Reconfiguration of an existing
    source is always allowed — adding/removing branches, switching extended on
    or off, rotating credentials, etc. The next sync will reconcile contents.
    """
    folder = check_owner(folder_id, db, user)
    existing = db.get(FolderSyncSource, folder_id)

    if existing is None and folder_has_real_files(db, folder_id, Path(folder.path)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Sync can only be configured on empty folders",
        )

    handler = registry.HANDLERS.get(body.source_type)
    if handler is None or handler.apply is None:  # pragma: no cover — Literal guards
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported source_type: {body.source_type!r}",
        )
    # db/user context lets credential-aware connectors (google_drive) validate
    # a referenced company credential; the others swallow it via **_.
    src = handler.apply(
        body=body, existing=existing, folder_id=folder_id, db=db, user=user
    )

    # Auto-sync settings apply regardless of source type.
    src.auto_sync_enabled = bool(body.auto_sync_enabled)
    src.auto_sync_hours = int(body.auto_sync_hours)

    if existing is None:
        db.add(src)
    db.commit()
    db.refresh(src)
    if existing is None:
        # Toolbar visibility & label depend on has_sync_source — push the
        # update so the UI doesn't need to refetch the folder list.
        publish_folder_changed(folder, has_sync_source=True)
    out = to_out(src)
    publish_sync_config_changed(folder_id, out)
    return out


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_sync_source(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        return
    db.delete(src)
    db.commit()
    publish_folder_changed(folder, has_sync_source=False)
    # config=None tells the client to drop any cached config for this folder.
    publish_sync_config_changed(folder_id, None)


@router.delete("/error", response_model=SyncSourceOut)
def clear_sync_error(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut:
    """Wipe ``sync_error`` and reset ``sync_status`` from 'error' to 'idle'.

    The error message is informational once the user has read it; we
    don't want a stale 'API not enabled' bullet from yesterday's run
    polluting the modal after the admin actually enabled the API.

    Idempotent: noop when there's no error to clear, no-op when there's
    no source row for this folder. Either way returns the (possibly-
    unchanged) source so the SPA can re-render from one response.
    """
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No sync source for this folder")
    if src.sync_error is not None or src.sync_status == "error":
        src.sync_error = None
        if src.sync_status == "error":
            src.sync_status = "idle"
        db.commit()
        db.refresh(src)
        events.publish(
            "folders",
            {
                "type": "folder.sync_source_changed",
                "folder_id": folder_id,
                "sync_status": src.sync_status,
                "sync_error": src.sync_error,
                "last_synced_at": src.last_synced_at,
            },
        )
        publish_sync_config_changed(folder_id, to_out(src))
    return to_out(src)


@router.post("/trigger", response_model=SyncTriggerOut)
def trigger_sync(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncTriggerOut:
    """Enqueue a sync job. Dedup'd per folder so spam-clicking is harmless."""
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No sync source configured"
        )
    handler = registry.HANDLERS.get(src.source_type)
    if handler is not None and handler.trigger_check is not None:
        handler.trigger_check(src)
    job_id = job_queue.enqueue(
        db,
        "sync",
        {"folder_id": folder_id},
        dedup_key=f"sync:{folder_id}",
    )
    db.commit()
    return SyncTriggerOut(folder_id=folder_id, job_id=job_id)
