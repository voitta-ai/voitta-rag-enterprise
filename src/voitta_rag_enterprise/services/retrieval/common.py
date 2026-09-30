"""Helpers shared by the retrieval modules: provenance, layout, image bytes."""

from __future__ import annotations

import io
import json
import logging
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...cas import store as cas_store
from ...db.models import ChunkImageLink, File
from ..file_classify import source_kind as classify_source_kind
from .models import FileInfo

logger = logging.getLogger(__name__)


def file_info(file: File) -> FileInfo:
    """Build a FileInfo with the classifier-derived ``source_kind`` filled in.

    Centralized so adding a new field to the wire model is a single-site
    change. The classifier is cheap (string-prefix match + dict lookup);
    don't bother caching.
    """
    return FileInfo(
        id=file.id,
        folder_id=file.folder_id,
        rel_path=file.rel_path,
        state=file.state,
        source_url=file.source_url,
        last_indexed_at=file.last_indexed_at,
        source_kind=classify_source_kind(file),
    )


def file_provenance(s: Session, file_id: int | None) -> tuple[str | None, str]:
    """Fetch ``(source_url, source_kind)`` for ``file_id`` in one query.

    Used by chunk/image hit builders to stamp file-level provenance onto
    every wire row. Returns ``(None, "other")`` when the file row is
    missing (e.g. stale Qdrant payload after a file deletion that Qdrant
    hasn't propagated yet).
    """
    if file_id is None:
        return (None, "other")
    file = s.get(File, file_id)
    if file is None:
        return (None, "other")
    return (file.source_url, classify_source_kind(file))


def nearby_image_ids(s: Session, chunk_id: int) -> list[int]:
    return [
        link.image_id
        for link in s.execute(
            select(ChunkImageLink).where(ChunkImageLink.chunk_id == chunk_id)
        )
        .scalars()
        .all()
    ]


def layout_from_payload(payload: dict) -> dict | None:
    """Re-collect the flat ``layout_*`` fields from a Qdrant payload back
    into a single dict for the LLM-facing schema.

    The indexer flattens ``layout_summary`` into top-level payload keys (one
    Qdrant payload index per scalar — see vector_store._chunk_payload) so
    each is independently filterable. The LLM doesn't filter, it reads, so a
    wrapped dict keeps the response surface tidy. Returns ``None`` when no
    layout fields are present (chunk indexed before the layout pipeline
    shipped, or non-PDF parser).
    """
    layout = {k: v for k, v in payload.items() if k.startswith("layout_")}
    return layout or None


@lru_cache(maxsize=64)
def _load_layout_blocks(file_cas_id: str) -> tuple[dict, ...]:
    """Read + parse a file's stored ``page_layout.json`` once per CAS sha.

    LRU-cached so a sweep over a 150-page document costs one JSON parse
    total. Tuple-of-dicts (immutable container) is what makes the cache
    safe to share across threads — callers index into it but don't mutate.
    Returns ``()`` when the file has no layout (older index, or a non-PDF
    parser).
    """
    try:
        raw = cas_store.read_file_blob(file_cas_id, "page_layout.json")
    except FileNotFoundError:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("page_layout.json unparseable for cas=%s", file_cas_id)
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(b for b in data if isinstance(b, dict))


def layout_for_page(file_cas_id: str, page: int) -> list[dict]:
    """Filter the cached layout list down to blocks on ``page``."""
    return [b for b in _load_layout_blocks(file_cas_id) if b.get("page") == page]


@lru_cache(maxsize=128)
def read_cas_text(cas_id: str) -> str:
    """Cached read of a file's stored markdown.

    Multiple chunk-image lookups against the same Workspace document re-read
    the same CAS blob; cache by sha. Returns empty string for a missing blob
    — the resolver just produces no results.
    """
    try:
        raw = cas_store.read_file_blob(cas_id, "text.md")
    except FileNotFoundError:
        return ""
    return raw.decode("utf-8", errors="replace")


def resize_for_response(data: bytes, mime: str, max_size: int) -> tuple[bytes, str]:
    """Downscale ``data`` so its long edge is at most ``max_size`` px.

    No-op fast paths:
      * ``max_size <= 0`` — caller wants the raw blob.
      * source already fits — return original bytes/mime unchanged.

    Otherwise: decode, LANCZOS-resize preserving aspect ratio, re-encode as
    WebP at quality 75 (matches the storage default for page renders so the
    format change is invisible for that case; figures get a small quality
    hit in exchange for a much smaller payload). Decode failures fall back
    to the original bytes — better to over-deliver pixels than drop the
    response.
    """
    if max_size <= 0:
        return data, mime
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as img:
            long_edge = max(img.width, img.height)
            if long_edge <= max_size:
                return data, mime
            scale = max_size / long_edge
            new_size = (
                max(1, round(img.width * scale)),
                max(1, round(img.height * scale)),
            )
            # WebP handles RGB/RGBA natively; collapse anything else
            # (palette, grayscale, CMYK) to one of those before resize.
            mode = img.mode
            if mode not in ("RGB", "RGBA"):
                target_mode = "RGBA" if mode in ("LA", "PA", "P") and (
                    "transparency" in img.info or mode in ("LA", "PA")
                ) else "RGB"
                img = img.convert(target_mode)
            resized = img.resize(new_size, PILImage.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="WEBP", quality=75, method=6)
            return buf.getvalue(), "image/webp"
    except Exception as e:
        logger.warning("resize_for_response failed (max_size=%d): %s", max_size, e)
        return data, mime
