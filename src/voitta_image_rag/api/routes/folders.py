"""Folder registration + reconciliation endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    path: str = Field(..., description="Absolute host path to watch.")
    display_name: str | None = None


class FolderOut(BaseModel):
    id: int
    path: str
    display_name: str
    source_type: str
    enabled: bool
    created_at: int


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
        created_at=f.created_at,
    )


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
    abs_path = Path(body.path).expanduser().resolve()
    if not abs_path.exists() or not abs_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Path does not exist or is not a directory: {abs_path}",
        )

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
