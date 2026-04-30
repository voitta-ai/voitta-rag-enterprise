"""Folder sync configuration + trigger endpoints.

Currently only GitHub is supported; the request/response shapes leave room for
other providers behind the ``source_type`` discriminator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db.models import File, Folder, FolderSyncSource
from ...services import events, job_queue
from ...services.acl import CurrentUser, user_can_see_folder
from ...services.sync.github import (
    GitAuth,
    coerce_branches_field,
    encode_branches_field,
    list_remote_branches,
)
from ..deps import current_user, db_session

router = APIRouter(prefix="/folders/{folder_id}/sync", tags=["sync"])


class GithubSyncIn(BaseModel):
    """Payload for ``PUT /folders/{id}/sync`` when ``source_type == 'github'``."""

    repo: str = Field(..., min_length=1)
    path: str = ""
    branches: list[str] = Field(default_factory=list)
    all_branches: bool = False
    extended: bool = False
    auth_method: Literal["ssh", "token", ""] = ""
    username: str = ""
    pat: str = ""
    ssh_key: str = ""


class SyncSourceIn(BaseModel):
    source_type: Literal["github"]
    github: GithubSyncIn | None = None


class GithubSyncOut(BaseModel):
    repo: str
    path: str
    branches: list[str]
    all_branches: bool
    extended: bool
    auth_method: str
    username: str
    has_pat: bool
    has_ssh_key: bool


class SyncSourceOut(BaseModel):
    folder_id: int
    source_type: str
    sync_status: str
    sync_error: str | None
    last_synced_at: int | None
    github: GithubSyncOut | None = None


def _to_out(src: FolderSyncSource) -> SyncSourceOut:
    gh = None
    if src.source_type == "github":
        gh = GithubSyncOut(
            repo=src.gh_repo or "",
            path=src.gh_path or "",
            branches=coerce_branches_field(src.gh_branches) or [],
            all_branches=bool(src.gh_all_branches),
            extended=bool(src.gh_extended),
            auth_method=src.gh_auth_method or "",
            username=src.gh_username or "",
            has_pat=bool(src.gh_pat),
            has_ssh_key=bool(src.gh_token),
        )
    return SyncSourceOut(
        folder_id=src.folder_id,
        source_type=src.source_type,
        sync_status=src.sync_status,
        sync_error=src.sync_error,
        last_synced_at=src.last_synced_at,
        github=gh,
    )


def _check_access(
    folder_id: int, db: Session, user: CurrentUser
) -> Folder:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return folder


def _folder_has_real_files(db: Session, folder_id: int, folder_root: Path) -> bool:
    """Empty = no indexed files AND no on-disk files (other than sync sidecars)."""
    from sqlalchemy import select

    has_db_files = (
        db.execute(
            select(File.id).where(
                File.folder_id == folder_id, File.state != "deleted"
            ).limit(1)
        ).first()
        is not None
    )
    if has_db_files:
        return True
    if not folder_root.exists():
        return False
    for entry in folder_root.iterdir():
        if entry.name in (".voitta_sources.json", ".voitta_timestamps.json"):
            continue
        return True
    return False


@router.get("", response_model=SyncSourceOut | None)
def get_sync_source(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut | None:
    _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    return _to_out(src) if src is not None else None


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
    folder = _check_access(folder_id, db, user)
    existing = db.get(FolderSyncSource, folder_id)

    if existing is None and _folder_has_real_files(db, folder_id, Path(folder.path)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Sync can only be configured on empty folders",
        )

    if body.source_type != "github":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported source_type: {body.source_type!r}",
        )
    if body.github is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Missing 'github' config for source_type='github'",
        )

    cfg = body.github
    if not cfg.all_branches and not cfg.branches:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Either set all_branches=true or pick at least one branch",
        )
    if cfg.auth_method not in ("", "ssh", "token"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid auth_method: {cfg.auth_method!r}",
        )

    src = existing or FolderSyncSource(folder_id=folder_id, source_type="github")
    src.source_type = "github"
    src.gh_repo = cfg.repo.strip()
    src.gh_path = cfg.path.strip("/")
    src.gh_branches = encode_branches_field(cfg.branches)
    src.gh_all_branches = cfg.all_branches
    src.gh_extended = cfg.extended
    src.gh_auth_method = cfg.auth_method or None
    src.gh_username = cfg.username or None
    src.gh_pat = cfg.pat or None
    src.gh_token = cfg.ssh_key or None
    if existing is None:
        db.add(src)

    db.commit()
    db.refresh(src)
    if existing is None:
        # Toolbar visibility & label depend on has_sync_source — push the
        # update so the UI doesn't need to refetch the folder list.
        _publish_folder_changed(folder, has_sync_source=True)
    return _to_out(src)


def _publish_folder_changed(folder: Folder, *, has_sync_source: bool) -> None:
    events.publish(
        "folders",
        {
            "type": "folder.upserted",
            "folder": {
                "id": folder.id,
                "path": folder.path,
                "display_name": folder.display_name,
                "source_type": folder.source_type,
                "enabled": folder.enabled,
                "managed": folder.managed,
                "created_at": folder.created_at,
                "has_sync_source": has_sync_source,
            },
        },
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_sync_source(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        return
    db.delete(src)
    db.commit()
    _publish_folder_changed(folder, has_sync_source=False)


class SyncTriggerOut(BaseModel):
    folder_id: int
    job_id: int


@router.post("/trigger", response_model=SyncTriggerOut)
def trigger_sync(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncTriggerOut:
    """Enqueue a sync job. Dedup'd per folder so spam-clicking is harmless."""
    _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No sync source configured"
        )
    job_id = job_queue.enqueue(
        db,
        "sync",
        {"folder_id": folder_id},
        dedup_key=f"sync:{folder_id}",
    )
    db.commit()
    return SyncTriggerOut(folder_id=folder_id, job_id=job_id)


class BranchListIn(BaseModel):
    repo: str
    auth_method: Literal["ssh", "token", ""] = ""
    username: str = ""
    pat: str = ""
    ssh_key: str = ""


class BranchListOut(BaseModel):
    branches: list[str]


@router.post("/branches", response_model=BranchListOut)
def list_branches(
    folder_id: int,
    body: BranchListIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> BranchListOut:
    """Helper for the UI's branch dropdown — runs ``git ls-remote --heads``.

    Auth is taken from the request body (the user is mid-form and the row
    might not have been saved yet).
    """
    _check_access(folder_id, db, user)
    auth = GitAuth(
        method=body.auth_method or "",
        ssh_key=body.ssh_key,
        username=body.username,
        pat=body.pat,
    )
    try:
        branches = list_remote_branches(body.repo.strip(), auth)
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"git ls-remote failed: {e}"
        ) from e
    return BranchListOut(branches=branches)
