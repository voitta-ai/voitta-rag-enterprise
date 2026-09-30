"""Hybrid text search and cross-modal image search, folder-scoped per viewer."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...db.database import session_scope
from ..embedding import get_image_embedder, get_sparse_embedder, get_text_embedder
from ..vector_store import SearchHit
from ..vector_store import search_chunks as vs_search_chunks
from ..vector_store import search_images as vs_search_images
from .common import file_provenance, layout_from_payload
from .models import ChunkInfo, ImageInfo
from .viewer import Viewer


def search(
    viewer: Viewer,
    query: str,
    folder_ids: list[int] | None = None,
    limit: int = 20,
) -> list[ChunkInfo]:
    """Dense + sparse RRF-fused chunk search (may include folder-card hits)."""
    limit = max(1, min(limit, 100))
    text_emb = get_text_embedder()
    sparse_emb = get_sparse_embedder()
    with session_scope() as s:
        hits = vs_search_chunks(
            dense=text_emb.embed_query(query),
            sparse=sparse_emb.embed_query(query),
            limit=limit,
            folder_ids=viewer.search_filter(s, folder_ids),
        )
        return [chunk_from_hit(h, s) for h in hits]


def search_images(
    viewer: Viewer,
    query: str,
    folder_ids: list[int] | None = None,
    limit: int = 20,
) -> list[ImageInfo]:
    """Text → image search through the image embedder's text encoder."""
    limit = max(1, min(limit, 100))
    image_emb = get_image_embedder()
    with session_scope() as s:
        hits = vs_search_images(
            vector=image_emb.embed_text(query),
            limit=limit,
            folder_ids=viewer.search_filter(s, folder_ids),
        )
        return [image_from_hit(h, s) for h in hits]


def chunk_from_hit(h: SearchHit, s: Session | None = None) -> ChunkInfo:
    p = h.payload
    if p.get("kind") == "folder_card":
        # Synthetic folder/subfolder hit — no file behind it. ``file_path``
        # carries the subpath inside the folder ('' = root); the card text
        # already spells out the folder name + description.
        return ChunkInfo(
            kind="folder_card",
            chunk_id=0,
            file_id=None,
            folder_id=p.get("folder_id"),
            file_path=str(p.get("subpath", "")),
            chunk_index=0,
            text=str(p.get("text", "")),
            score=h.score,
            source_kind="folder",
        )
    file_id = int(p["file_id"])
    source_url, source_kind = (None, "other")
    if s is not None:
        source_url, source_kind = file_provenance(s, file_id)
    return ChunkInfo(
        chunk_id=int(p.get("chunk_id", h.id)),
        file_id=file_id,
        folder_id=p.get("folder_id"),
        file_path=str(p.get("file_path", "")),
        chunk_index=int(p.get("chunk_index", 0)),
        text=str(p.get("text", "")),
        nearby_image_ids=list(p.get("nearby_image_ids") or []),
        score=h.score,
        page=p.get("page"),
        pages=list(p.get("pages") or []),
        layout=layout_from_payload(p),
        source_url=source_url,
        source_kind=source_kind,
    )


def image_from_hit(h: SearchHit, s: Session | None = None) -> ImageInfo:
    """Build an ImageInfo from a Qdrant search hit.

    ``width`` / ``height`` / ``mime`` are deliberately not populated: they
    live on the Image DB row, not in the Qdrant payload, and the search path
    doesn't read the DB. Callers that need them resolve via
    ``get_chunk_images`` / ``list_page_images`` — both read the DB.
    """
    p = h.payload
    file_id = int((p.get("file_ids") or [0])[0])
    source_url, source_kind = (None, "other")
    if s is not None:
        source_url, source_kind = file_provenance(s, file_id)
    return ImageInfo(
        image_id=int(p.get("image_id", h.id)),
        file_id=file_id,
        file_path=str(p.get("file_path", "")),
        image_cas_id=str(p.get("image_cas_id", "")),
        page=p.get("page"),
        score=h.score,
        layout=layout_from_payload(p),
        source_url=source_url,
        source_kind=source_kind,
    )
