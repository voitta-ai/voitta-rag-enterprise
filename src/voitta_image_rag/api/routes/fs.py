"""Filesystem browser. Read-only directory listing for the folder picker."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ...services.acl import CurrentUser
from ..deps import current_user

router = APIRouter(prefix="/fs", tags=["fs"])


class FsEntry(BaseModel):
    name: str
    is_dir: bool


class FsListing(BaseModel):
    path: str
    parent: str | None
    entries: list[FsEntry]


def _safe_resolve(raw: str | None) -> Path:
    target = Path(raw).expanduser().resolve() if raw else Path(os.path.expanduser("~"))
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such path: {target}")
    if not target.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a directory: {target}")
    return target


@router.get("/list", response_model=FsListing)
def list_dir(
    path: str | None = Query(default=None, description="Absolute path; default = $HOME"),
    user: CurrentUser = Depends(current_user),
) -> FsListing:
    target = _safe_resolve(path)
    parent = str(target.parent) if target.parent != target else None
    entries: list[FsEntry] = []
    try:
        children = sorted(
            target.iterdir(),
            key=lambda p: (not _is_dir_safe(p), p.name.lower()),
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Permission denied: {target}") from e

    for child in children:
        try:
            entries.append(FsEntry(name=child.name, is_dir=_is_dir_safe(child)))
        except OSError:
            continue
    return FsListing(path=str(target), parent=parent, entries=entries)


def _is_dir_safe(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False
