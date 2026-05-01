"""Search endpoint — text → chunks/images, with optional folder filter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...config import get_settings
from ...services.acl import CurrentUser, visible_folder_ids
from ...services.embedding import get_image_embedder, get_sparse_embedder, get_text_embedder
from ...services.vector_store import search_chunks, search_images
from ..deps import current_user, db_session

router = APIRouter(prefix="/search", tags=["search"])


class SearchIn(BaseModel):
    query: str
    modes: list[str] = Field(default_factory=lambda: ["chunks", "images"])
    folder_ids: list[int] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class Hit(BaseModel):
    id: int
    score: float
    payload: dict[str, Any]


class SearchOut(BaseModel):
    chunks: list[Hit] = Field(default_factory=list)
    images: list[Hit] = Field(default_factory=list)


def _resolve_folder_ids(
    db: Session, user_id: int, requested: list[int] | None
) -> list[int] | None:
    """Compute the folder-id filter for a search.

    Identity-side filter: the user's visible folders (owned, ACL-granted,
    shared). When the caller passes ``folder_ids`` we intersect with the
    visible set so a malicious request can't exfiltrate from another
    user's folder.

    Returns ``None`` only in single-user mode, where Qdrant skips the
    filter entirely.
    """
    if get_settings().single_user:
        # Pass-through: caller-explicit list is honoured, else None means
        # "no filter" which lets the worker scan everything.
        return requested
    visible = set(visible_folder_ids(db, user_id))
    if not visible:
        # No visible folders → nothing to search. Returning a tautologically-
        # empty filter (an impossible folder id) gives Qdrant a cheap "no
        # match" path while keeping the type as a list.
        return [-1]
    if requested is None:
        return sorted(visible)
    intersect = [fid for fid in requested if fid in visible]
    return intersect or [-1]


@router.post("", response_model=SearchOut)
def search(
    body: SearchIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SearchOut:
    folder_ids = _resolve_folder_ids(db, user.id, body.folder_ids)
    out = SearchOut()
    if "chunks" in body.modes:
        text_emb = get_text_embedder()
        sparse_emb = get_sparse_embedder()
        dense = text_emb.embed_query(body.query)
        sparse = sparse_emb.embed_query(body.query)
        hits = search_chunks(
            dense=dense,
            sparse=sparse,
            limit=body.limit,
            folder_ids=folder_ids,
        )
        out.chunks = [Hit(id=h.id, score=h.score, payload=h.payload) for h in hits]
    if "images" in body.modes:
        image_emb = get_image_embedder()
        vec = image_emb.embed_text(body.query)
        hits = search_images(
            vector=vec,
            limit=body.limit,
            folder_ids=folder_ids,
        )
        out.images = [Hit(id=h.id, score=h.score, payload=h.payload) for h in hits]
    return out
