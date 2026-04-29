"""Search endpoint — text → chunks/images, with optional folder filter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...services.acl import CurrentUser
from ...services.embedding import get_image_embedder, get_sparse_embedder, get_text_embedder
from ...services.vector_store import search_chunks, search_images
from ..deps import current_user

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


@router.post("", response_model=SearchOut)
def search(body: SearchIn, user: CurrentUser = Depends(current_user)) -> SearchOut:
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
            folder_ids=body.folder_ids,
            allowed_user_id=user.id,
        )
        out.chunks = [Hit(id=h.id, score=h.score, payload=h.payload) for h in hits]
    if "images" in body.modes:
        image_emb = get_image_embedder()
        vec = image_emb.embed_text(body.query)
        hits = search_images(
            vector=vec,
            limit=body.limit,
            folder_ids=body.folder_ids,
            allowed_user_id=user.id,
        )
        out.images = [Hit(id=h.id, score=h.score, payload=h.payload) for h in hits]
    return out
