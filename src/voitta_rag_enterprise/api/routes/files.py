"""File detail + content endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...cas import store as cas_store
from ...db.models import Chunk, File, Image
from ...services.acl import CurrentUser
from ..deps import current_user, db_session

router = APIRouter(prefix="/files", tags=["files"])


class FileDetail(BaseModel):
    id: int
    folder_id: int
    rel_path: str
    state: str
    size_bytes: int | None
    mtime_ns: int | None
    last_indexed_at: int | None
    source_url: str | None
    tab: str | None
    file_cas_id: str | None
    pending_embeds: int


class FileImage(BaseModel):
    image_id: int
    image_index: int
    position: int | None
    page: int | None
    width: int | None
    height: int | None
    mime: str | None
    image_cas_id: str


def _to_file_detail(f: File) -> FileDetail:
    return FileDetail(
        id=f.id,
        folder_id=f.folder_id,
        rel_path=f.rel_path,
        state=f.state,
        size_bytes=f.size_bytes,
        mtime_ns=f.mtime_ns,
        last_indexed_at=f.last_indexed_at,
        source_url=f.source_url,
        tab=f.tab,
        file_cas_id=f.file_cas_id,
        pending_embeds=f.pending_embeds,
    )


@router.get("/{file_id}", response_model=FileDetail)
def get_file(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FileDetail:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return _to_file_detail(file)


@router.get("/{file_id}/text", response_class=PlainTextResponse)
def get_file_text(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> str:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if not file.file_cas_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "File not yet extracted")
    try:
        return cas_store.read_file_blob(file.file_cas_id, "text.md").decode("utf-8")
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extracted text missing") from e


@router.get("/{file_id}/images", response_model=list[FileImage])
def get_file_images(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FileImage]:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    # SPA renders these inline next to their anchor chunk; page renders
    # have no anchor and would just clutter the carousel. Page renders are
    # served separately via the MCP list_page_images / get_page_image tools.
    rows = (
        db.execute(
            select(Image)
            .where(Image.file_id == file_id, Image.kind == "figure")
            .order_by(Image.image_index)
        )
        .scalars()
        .all()
    )
    out: list[FileImage] = []
    for img in rows:
        # The anchor chunk's char_start is the splice point for inline rendering.
        # Standalone images have no anchor, so position stays None.
        position = None
        if img.anchor_chunk is not None:
            anchor = db.execute(
                select(Chunk).where(
                    Chunk.file_id == file_id, Chunk.chunk_index == img.anchor_chunk
                )
            ).scalar_one_or_none()
            position = anchor.char_start if anchor is not None else None
        out.append(
            FileImage(
                image_id=img.id,
                image_index=img.image_index,
                position=position,
                page=img.page,
                width=img.width,
                height=img.height,
                mime=img.mime,
                image_cas_id=img.image_cas_id,
            )
        )
    return out
