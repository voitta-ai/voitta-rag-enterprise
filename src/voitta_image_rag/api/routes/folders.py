"""Folder registration + reconciliation endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import File, Folder
from ...services import events
from ...services.acl import (
    CurrentUser,
    grant_folder,
    revoke_folder,
    user_can_see_folder,
    visible_folder_ids,
)
from ...services.scanner import scan_folder
from ...services.watcher import unwatch_folder_in_default, watch_folder_in_default
from ..deps import current_user, db_session

router = APIRouter(prefix="/folders", tags=["folders"])


class FolderIn(BaseModel):
    """Two modes:

    - **External**: pass ``path`` (absolute host path that already exists).
      ``managed=False``; never gets a sync connector.
    - **Managed**: pass ``name`` (single path segment). Server creates
      ``$VOITTA_ROOT_PATH/<name>`` if missing. ``managed=True``; sync
      connectors can later be attached.
    """

    path: str | None = Field(default=None, description="Absolute host path (external mode)")
    name: str | None = Field(default=None, description="Folder name under VOITTA_ROOT_PATH (managed mode)")
    display_name: str | None = None


class FolderOut(BaseModel):
    id: int
    path: str
    display_name: str
    source_type: str
    enabled: bool
    managed: bool
    created_at: int


class RootInfo(BaseModel):
    root_path: str | None
    configured: bool


class FileOut(BaseModel):
    id: int
    rel_path: str
    state: str
    size_bytes: int | None
    mtime_ns: int | None
    last_indexed_at: int | None
    source_url: str | None


def _to_folder_out(f: Folder) -> FolderOut:
    return FolderOut(
        id=f.id,
        path=f.path,
        display_name=f.display_name,
        source_type=f.source_type,
        enabled=f.enabled,
        managed=f.managed,
        created_at=f.created_at,
    )


def _resolve_managed(name: str) -> Path:
    """Validate ``name`` and return the absolute path it should occupy.

    Raises ``HTTPException`` on misconfig or unsafe input.
    """
    settings = get_settings()
    if settings.root_path is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Managed folders require VOITTA_ROOT_PATH to be configured.",
        )
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid folder name: {name!r}",
        )
    root = Path(settings.root_path).expanduser().resolve()
    target = (root / name).resolve()
    # Defence in depth against ``../`` slipping past the substring check.
    if root != target.parent:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Folder name escapes root: {name!r}",
        )
    return target


def _to_file_out(f: File) -> FileOut:
    return FileOut(
        id=f.id,
        rel_path=f.rel_path,
        state=f.state,
        size_bytes=f.size_bytes,
        mtime_ns=f.mtime_ns,
        last_indexed_at=f.last_indexed_at,
        source_url=f.source_url,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FolderOut)
def create_folder(
    body: FolderIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderOut:
    if bool(body.path) == bool(body.name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide exactly one of: 'path' (external) or 'name' (managed under VOITTA_ROOT_PATH)",
        )

    if body.name is not None:
        abs_path = _resolve_managed(body.name)
        abs_path.mkdir(parents=True, exist_ok=True)
        managed = True
    else:
        abs_path = Path(body.path).expanduser().resolve()
        if not abs_path.exists() or not abs_path.is_dir():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Path does not exist or is not a directory: {abs_path}",
            )
        managed = False

    existing = db.execute(
        select(Folder).where(Folder.path == str(abs_path))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Folder already registered (id={existing.id})"
        )

    folder = Folder(
        path=str(abs_path),
        display_name=body.display_name or abs_path.name or str(abs_path),
        source_type="filesystem",
        managed=managed,
    )
    db.add(folder)
    db.flush()
    grant_folder(db, folder.id, user.id)
    scan_folder(db, folder)
    db.commit()
    watch_folder_in_default(folder)
    out = _to_folder_out(folder)
    events.publish("folders", {"type": "folder.added", "folder": out.model_dump()})
    return out


@router.get("/root", response_model=RootInfo)
def folder_root(
    user: CurrentUser = Depends(current_user),
) -> RootInfo:
    """Where managed folders are created, or null if not configured."""
    settings = get_settings()
    if settings.root_path is None:
        return RootInfo(root_path=None, configured=False)
    return RootInfo(root_path=str(settings.root_path), configured=True)


class GrantBody(BaseModel):
    user_id: int


@router.post("/{folder_id}/grant", status_code=status.HTTP_204_NO_CONTENT)
def grant(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    grant_folder(db, folder_id, body.user_id)
    db.commit()


@router.post("/{folder_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    revoke_folder(db, folder_id, body.user_id)
    db.commit()


@router.get("", response_model=list[FolderOut])
def list_folders(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FolderOut]:
    visible = set(visible_folder_ids(db, user.id))
    rows = db.execute(select(Folder).order_by(Folder.id)).scalars().all()
    return [_to_folder_out(f) for f in rows if f.id in visible]


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    db.delete(folder)
    db.commit()
    unwatch_folder_in_default(folder_id)
    events.publish("folders", {"type": "folder.removed", "folder_id": folder_id})


@router.get("/{folder_id}/files", response_model=list[FileOut])
def list_folder_files(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FileOut]:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    rows = (
        db.execute(
            select(File).where(File.folder_id == folder_id).order_by(File.rel_path)
        )
        .scalars()
        .all()
    )
    return [_to_file_out(f) for f in rows]
