"""MCP server.

Exposes the same data the HTTP API does, but as MCP tools that an LLM agent
can call.

Authentication
--------------
Clients authenticate with a personal API key minted from the SPA's Settings
panel and presented as ``Authorization: Bearer vk_…``. The middleware
resolves that token to a user, sets the resolved email on a ContextVar that
every tool reads through ``_resolved_user_id``, and bumps ``last_used_at``.
A request with a missing or invalid bearer is rejected with 401.

The ``VOITTA_SINGLE_USER`` and ``VOITTA_DEV_USER`` env modes still bypass
authentication: those are local-dev shortcuts and the bearer requirement
would only get in the way. In production neither is set, so every MCP call
must carry a valid bearer.

Run standalone::

    python -m voitta_rag_enterprise.mcp_server
"""

from __future__ import annotations

import base64
import json
import logging
from contextvars import ContextVar
from functools import lru_cache

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .cas import store as cas_store
from .config import get_settings
from .db.database import init_db, session_scope
from .db.models import Chunk, ChunkImageLink, File, Folder, Image
from .services.acl import (
    ROOT_EMAIL,
    get_or_create_user,
    mcp_visible_folder_ids,
    visible_folder_ids,
)
from .services.embedding import (
    get_image_embedder,
    get_sparse_embedder,
    get_text_embedder,
)
from .services.vector_store import (
    SearchHit,
)
from .services.vector_store import (
    search_chunks as vs_search_chunks,
)
from .services.vector_store import (
    search_images as vs_search_images,
)

logger = logging.getLogger(__name__)

_current_user: ContextVar[str | None] = ContextVar("voitta_mcp_user", default=None)

mcp = FastMCP("voitta-rag-enterprise")


def _resolved_user() -> str:
    """Mirror the HTTP ACL resolver.

    Priority — first match wins:

    1. ``VOITTA_SINGLE_USER`` → ``root@localhost`` (local dev)
    2. ``VOITTA_DEV_USER`` → that email (local dev)
    3. ContextVar set by ``BearerAuthMiddleware`` from a verified API key
    4. ``"anonymous"`` (only reachable via direct in-process tool calls;
       network requests get rejected by the middleware before reaching here)
    """
    s = get_settings()
    if s.single_user:
        return ROOT_EMAIL
    if s.dev_user:
        return s.dev_user
    return _current_user.get() or "anonymous"


def _resolved_user_id() -> int | None:
    """Resolve the caller's user id, creating the user row on first call.

    Returns ``None`` when single-user mode is on — the search-time ACL filter
    becomes a no-op.
    """
    if get_settings().single_user:
        return None
    email = _resolved_user()
    if email == "anonymous":
        return None
    with session_scope() as s:
        user = get_or_create_user(s, email)
        return user.id


# ---------------------------------------------------------------------------
# Pydantic schemas — mirror the HTTP responses but trimmed for LLM friendliness
# ---------------------------------------------------------------------------


class FolderInfo(BaseModel):
    id: int
    path: str
    display_name: str
    source_type: str
    files_total: int
    files_indexed: int
    # True when this folder is currently included in the caller's MCP search
    # (i.e. they haven't toggled it off in Settings). Always True in single-
    # user / dev-user modes.
    active: bool = True
    # True when the folder is shared globally (owner toggled the switch).
    shared: bool = False


class FileInfo(BaseModel):
    id: int
    folder_id: int
    rel_path: str
    state: str
    # Source URL set by the sync connectors (Google Drive deep-link,
    # GitHub raw URL, …). None for files indexed from a local path.
    source_url: str | None = None
    # Unix epoch seconds of the last successful indexing pass. None
    # for files that have never reached state='indexed'.
    last_indexed_at: int | None = None


# Field-presence policy across every MCP response model: declared
# optional fields always appear on the wire with their declared value
# (``null``/empty for unknowns). No custom serializer strips them. The
# JSON schema FastMCP advertises matches the wire format byte-for-byte,
# so client-side structured-content validators don't have to special-case
# "absent vs null". Adding a new field means giving it a default — never
# a serializer.


class ChunkInfo(BaseModel):
    chunk_id: int
    file_id: int
    file_path: str
    chunk_index: int
    text: str
    # Image ids whose extracted figure overlaps this chunk's page span.
    # Empty list for non-PDF chunks (only the PDF pipeline links chunks
    # to figures); the field is always emitted.
    nearby_image_ids: list[int] = Field(default_factory=list)
    # Search hit score (dense + sparse RRF). None on chunk-range / get-file
    # responses where there's no ranking context.
    score: float | None = None
    # Page anchoring + layout summary, attached at index time. ``page``
    # is the chunk's primary (start-anchored) page; ``pages`` is every
    # page the chunk touches. ``layout`` is the per-page summary dict
    # (``layout_kind``, ``layout_has_image``/``_table``, ``layout_n_*``,
    # …) — mirror of what search filters can match on. All None / empty
    # for chunks from non-PDF parsers (text/code/markdown/...).
    page: int | None = None
    pages: list[int] = Field(default_factory=list)
    layout: dict | None = None


class ImageInfo(BaseModel):
    image_id: int
    file_id: int
    file_path: str
    image_cas_id: str
    # PDF page number this figure sits on. None for figures from non-PDF
    # parsers (DOCX/PPTX images, standalone uploads) and for search hits
    # where the indexer hasn't stored a page reference.
    page: int | None = None
    # Pixel dimensions + mime are stored on the Image DB row; search
    # payloads don't carry them, so they're None on search hits and
    # populated on metadata-direct paths (get_chunk_images, list_page_images).
    width: int | None = None
    height: int | None = None
    mime: str | None = None
    # 'figure' (cropped extract) or 'page_render' (full-page raster).
    # search_images / get_chunk_images return only figures; page renders
    # are surfaced via list_page_images / get_page_image.
    kind: str = "figure"
    # Search hit score. None on non-search paths.
    score: float | None = None
    # Per-page layout summary for the page this image sits on. None for
    # images that don't come from the PDF pipeline.
    layout: dict | None = None


class PageImageInfo(BaseModel):
    """Catalog entry for a per-page render. Bytes are fetched separately."""

    image_id: int
    file_id: int
    page: int
    # Width / height / mime are stored on the Image row in DB; these
    # endpoints read from there, so the values are normally populated.
    # Kept nullable to cover legacy rows pre-dating the metadata write.
    width: int | None = None
    height: int | None = None
    mime: str | None = None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_indexed_folders() -> list[FolderInfo]:
    """List the folders visible to the calling user, with file-count breakdown.

    Returns every visible folder (owned, granted, shared). The ``active``
    flag tells the caller which ones are currently in their MCP search
    rotation — they remain listed even when toggled off so the LLM can
    suggest re-enabling.
    """
    settings = get_settings()
    user_id = _resolved_user_id()
    show_all = settings.single_user
    with session_scope() as s:
        if show_all or user_id is None:
            visible_ids: set[int] = {
                f.id for f in s.execute(select(Folder)).scalars()
            }
            active_ids = visible_ids
        else:
            visible_ids = set(visible_folder_ids(s, user_id))
            active_ids = set(mcp_visible_folder_ids(s, user_id))
        out: list[FolderInfo] = []
        for f in s.execute(select(Folder).order_by(Folder.id)).scalars():
            if f.id not in visible_ids:
                continue
            total = (
                s.execute(
                    select(File).where(File.folder_id == f.id, File.state != "deleted")
                )
                .scalars()
                .all()
            )
            indexed = [x for x in total if x.state == "indexed"]
            out.append(
                FolderInfo(
                    id=f.id,
                    path=f.path,
                    display_name=f.display_name,
                    source_type=f.source_type,
                    files_total=len(total),
                    files_indexed=len(indexed),
                    active=f.id in active_ids,
                    shared=bool(f.shared),
                )
            )
        return out


def _mcp_search_folder_filter(
    s, user_id: int | None, requested: list[int] | None
) -> list[int] | None:
    """Same shape as the REST helper but uses ``mcp_visible_folder_ids`` so
    the user's per-folder ``active`` toggle is enforced."""
    if get_settings().single_user or user_id is None:
        return requested
    active = set(mcp_visible_folder_ids(s, user_id))
    if not active:
        return [-1]
    if requested is None:
        return sorted(active)
    intersect = [fid for fid in requested if fid in active]
    return intersect or [-1]


@mcp.tool()
def search(
    query: str,
    folder_ids: list[int] | None = None,
    limit: int = 20,
) -> list[ChunkInfo]:
    """Hybrid (dense + sparse, RRF-fused) search over text chunks.

    :param query: free-text query
    :param folder_ids: restrict search to these folder ids
    :param limit: max hits (1..100)
    """
    limit = max(1, min(limit, 100))
    text_emb = get_text_embedder()
    sparse_emb = get_sparse_embedder()
    user_id = _resolved_user_id()
    with session_scope() as s:
        effective = _mcp_search_folder_filter(s, user_id, folder_ids)
    hits = vs_search_chunks(
        dense=text_emb.embed_query(query),
        sparse=sparse_emb.embed_query(query),
        limit=limit,
        folder_ids=effective,
    )
    return [_chunk_from_hit(h) for h in hits]


@mcp.tool()
def search_images(
    query: str,
    folder_ids: list[int] | None = None,
    limit: int = 20,
) -> list[ImageInfo]:
    """Cross-modal text→image search via the image embedder's text encoder."""
    limit = max(1, min(limit, 100))
    image_emb = get_image_embedder()
    user_id = _resolved_user_id()
    with session_scope() as s:
        effective = _mcp_search_folder_filter(s, user_id, folder_ids)
    hits = vs_search_images(
        vector=image_emb.embed_text(query),
        limit=limit,
        folder_ids=effective,
    )
    return [_image_from_hit(h) for h in hits]


@mcp.tool()
def get_file(file_id: int) -> dict:
    """Return file metadata + the full extracted markdown content."""
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None:
            raise ValueError(f"File {file_id} not found")
        info = FileInfo(
            id=f.id,
            folder_id=f.folder_id,
            rel_path=f.rel_path,
            state=f.state,
            source_url=f.source_url,
            last_indexed_at=f.last_indexed_at,
        )
        cas_id = f.file_cas_id

    text = ""
    if cas_id:
        try:
            text = cas_store.read_file_blob(cas_id, "text.md").decode("utf-8")
        except FileNotFoundError:
            text = ""
    return {"file": info.model_dump(), "text": text}


@mcp.tool()
def get_chunk_range(
    file_id: int,
    start_index: int = 0,
    end_index: int = 10,
) -> list[ChunkInfo]:
    """Return chunks ``[start_index, end_index)`` of a file, in order.

    Each chunk also carries the same ``page`` / ``pages`` / ``layout``
    fields as search hits, so the LLM can navigate "show me chunks on
    pages with tables" without going back through ``search``.
    """
    from .services.indexing import _load_char_to_page, _load_layout_summaries
    from .services.layout import pages_for_range, primary_page_for_range

    start_index = max(0, start_index)
    end_index = min(end_index, start_index + 500)
    if end_index <= start_index:
        return []
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None:
            raise ValueError(f"File {file_id} not found")
        file_cas_id = f.file_cas_id
        rel_path = f.rel_path
        rows = list(
            s.execute(
                select(Chunk)
                .where(
                    Chunk.file_id == file_id,
                    Chunk.chunk_index >= start_index,
                    Chunk.chunk_index < end_index,
                )
                .order_by(Chunk.chunk_index)
            ).scalars()
        )
        chunk_data = [
            (
                c.id,
                c.chunk_index,
                c.text,
                c.char_start,
                c.char_end,
                _nearby_image_ids(s, c.id),
            )
            for c in rows
        ]

    char_to_page = _load_char_to_page(file_cas_id)
    layout_summaries = _load_layout_summaries(file_cas_id)
    out: list[ChunkInfo] = []
    for cid, idx, text, c_start, c_end, nearby in chunk_data:
        primary = (
            primary_page_for_range(char_to_page, c_start or 0, c_end or 0)
            if char_to_page
            else None
        )
        pages = (
            pages_for_range(
                char_to_page, c_start or 0, c_end or (c_start or 0) + 1
            )
            if char_to_page
            else []
        )
        out.append(
            ChunkInfo(
                chunk_id=cid,
                file_id=file_id,
                file_path=rel_path,
                chunk_index=idx,
                text=text,
                nearby_image_ids=nearby,
                page=primary,
                pages=pages,
                layout=layout_summaries.get(primary) if primary else None,
            )
        )
    return out


@mcp.tool()
def get_chunk_images(chunk_id: int) -> list[ImageInfo]:
    """Return the figure images linked to ``chunk_id`` (within the configured radius).

    Page renders are not linked — fetch them via ``list_page_images`` /
    ``get_page_image`` instead.
    """
    with session_scope() as s:
        chunk = s.get(Chunk, chunk_id)
        if chunk is None:
            raise ValueError(f"Chunk {chunk_id} not found")
        file = s.get(File, chunk.file_id)
        rows = list(
            s.execute(
                select(Image, ChunkImageLink.distance)
                .join(ChunkImageLink, ChunkImageLink.image_id == Image.id)
                .where(ChunkImageLink.chunk_id == chunk_id)
                .order_by(ChunkImageLink.distance)
            )
        )
        return [
            ImageInfo(
                image_id=img.id,
                file_id=img.file_id,
                file_path=file.rel_path if file else "",
                image_cas_id=img.image_cas_id,
                page=img.page,
                width=img.width,
                height=img.height,
                mime=img.mime,
                kind=img.kind,
                score=float(distance),
            )
            for (img, distance) in rows
        ]


@mcp.tool()
def get_image(image_id: int, max_size: int = 420) -> dict:
    """Return image bytes (base64) + mime for inline rendering by an MCP client.

    Works for both figures and page renders — the caller already has the
    id from search hits, ``get_chunk_images``, or ``list_page_images``.

    ``max_size`` clamps the long edge of the returned raster: if the
    stored image is larger, it's downscaled with LANCZOS and re-encoded
    as WebP @ q75 before base64. The default is a thumbnail-grade 420px
    so the LLM doesn't drown in pixels it doesn't need; ask for more
    (e.g. 1024) when the detail actually matters. Pass ``0`` to skip
    resizing entirely and get the original bytes/mime.
    """
    with session_scope() as s:
        img = s.get(Image, image_id)
        if img is None:
            raise ValueError(f"Image {image_id} not found")
        cas_id = img.image_cas_id
        mime = img.mime or "application/octet-stream"
    try:
        data = cas_store.read_image_blob(cas_id)
    except FileNotFoundError as e:
        raise ValueError(f"Image bytes missing for {image_id}") from e
    data, mime = _resize_for_response(data, mime, max_size)
    return {
        "image_id": image_id,
        "mime": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


@mcp.tool()
def list_page_images(file_id: int) -> list[PageImageInfo]:
    """List per-page renders for ``file_id``, in page order.

    Returns layout-context rasters (typically WebP, ~1024px on the long
    edge). Use the returned ``image_id`` with ``get_page_image`` to fetch
    bytes. Files that weren't rendered (non-PDF, or PDFs indexed before
    page-rendering was enabled) return an empty list.
    """
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Image)
                .where(Image.file_id == file_id, Image.kind == "page_render")
                .order_by(Image.page, Image.image_index)
            ).scalars()
        )
        return [
            PageImageInfo(
                image_id=img.id,
                file_id=img.file_id,
                page=img.page or 0,
                width=img.width,
                height=img.height,
                mime=img.mime,
            )
            for img in rows
        ]


@mcp.tool()
def get_page_image(
    file_id: int,
    page: int,
    max_size: int = 420,
    include_layout: bool = True,
) -> dict:
    """Return bytes (base64) of the page-render for ``(file_id, page)``.

    Pages are 1-indexed. Raises ``ValueError`` if no render exists for
    that combination — typically because the file isn't a PDF or was
    indexed before per-page rendering was enabled.

    ``max_size`` clamps the long edge: stored renders are at ~1024px, so
    the default 420px gives a thumbnail-grade preview that's plenty for
    layout context while keeping payload small. Crank it up (e.g. 1024)
    when you actually need to read the typography. Pass ``0`` to skip
    resizing.

    ``include_layout`` (default True) attaches a ``layout`` field with
    the parser's per-block list for this page — types are MinerU's
    (``text``, ``title``, ``image``, ``table``, ``equation``, ...) plus
    coordinates in PDF points (top-left origin) when the parser
    provides them. ``layout`` is an empty list when the file was
    indexed before layout capture was added, or for non-PDF files. Set
    ``include_layout=False`` to skip the read + JSON parse.
    """
    with session_scope() as s:
        img = s.execute(
            select(Image)
            .where(
                Image.file_id == file_id,
                Image.kind == "page_render",
                Image.page == page,
            )
            .limit(1)
        ).scalar_one_or_none()
        if img is None:
            raise ValueError(
                f"No page render for file_id={file_id} page={page}"
            )
        image_id = img.id
        cas_id = img.image_cas_id
        mime = img.mime or "image/webp"
        file_cas_id = None
        if include_layout:
            file = s.get(File, file_id)
            file_cas_id = file.file_cas_id if file else None
    try:
        data = cas_store.read_image_blob(cas_id)
    except FileNotFoundError as e:
        raise ValueError(
            f"Page-render bytes missing for file_id={file_id} page={page}"
        ) from e
    data, mime = _resize_for_response(data, mime, max_size)
    layout: list[dict] = []
    if include_layout and file_cas_id:
        layout = _layout_for_page(file_cas_id, page)
    return {
        "image_id": image_id,
        "file_id": file_id,
        "page": page,
        "mime": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "layout": layout,
    }


@mcp.tool()
def get_page_layout(file_id: int, page: int) -> list[dict]:
    """Return the structured layout (per-block list) for ``(file_id, page)``.

    Same data as the ``layout`` field on ``get_page_image`` but without
    the image bytes — preferable when the LLM only needs to reason
    about page structure (which blocks exist, what types, what's
    above/below) and not see the actual rendering. Pages are 1-indexed.
    Returns an empty list if the file was indexed before layout capture
    or isn't a PDF.

    Each block carries at least ``type`` and ``page``; PDFs from MinerU
    typically also include ``bbox`` (PDF points, top-left origin),
    ``text`` for text/title blocks, and ``img_path`` for image/table
    blocks. Other fields are passed through as-is.
    """
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            raise ValueError(f"File {file_id} not found")
        file_cas_id = file.file_cas_id
    if not file_cas_id:
        return []
    return _layout_for_page(file_cas_id, page)


@mcp.tool()
def get_workbook(file_id: int) -> dict:
    """Return the full ``.xlsx`` workbook for a Sheets-derived markdown
    summary as base64-encoded bytes.

    Per-sheet markdown summaries cap at 100 rows; this tool is the
    escape hatch for callers that need the full grid. The workbook
    lives under ``<folder>/.voitta_workbooks/<rel>/...xlsx`` (excluded
    from indexing) and is written by the Sheets exporter on every
    sync.

    ``file_id`` is the id of any per-sheet ``.md`` file produced by
    the SpreadsheetExporter. The tool walks back to the workbook stem
    (the parent directory) and serves the matching xlsx. Raises if
    the file isn't a Sheets-derived markdown or the xlsx isn't on
    disk (e.g. an older sync from before this feature shipped — the
    user should re-sync the folder).
    """
    from pathlib import Path

    from .db.models import Folder

    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None:
            raise ValueError(f"File {file_id} not found")
        rel_path = Path(f.rel_path)
        folder = s.get(Folder, f.folder_id)
        if folder is None:
            raise ValueError(f"File {file_id} has no folder")
        folder_path = Path(folder.path)

    # Per-sheet md path: ``<some-dir>/<workbook stem>/NN-<sheet>.md``.
    # Workbook xlsx: ``.voitta_workbooks/<some-dir>/<workbook stem>.xlsx``.
    if rel_path.suffix.lower() != ".md" or rel_path.parent == Path(""):
        raise ValueError(
            f"File {file_id} ({rel_path}) is not a Sheets-derived markdown file"
        )
    workbook_rel = rel_path.parent  # e.g. "MyFolder/Q4 Plan"
    xlsx_path = folder_path / ".voitta_workbooks" / workbook_rel.with_suffix(".xlsx")
    if not xlsx_path.exists():
        raise FileNotFoundError(
            f"Workbook xlsx not found at {xlsx_path}. The folder may need to be "
            f"re-synced — pre-exporter syncs didn't write the .voitta_workbooks "
            f"sidecar."
        )

    data = xlsx_path.read_bytes()
    return {
        "file_id": file_id,
        "filename": xlsx_path.name,
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "data_base64": base64.b64encode(data).decode("ascii"),
        "size_bytes": len(data),
    }


@mcp.tool()
def resolve_url(url: str) -> list[FileInfo]:
    """Reverse-lookup an external URL (set by sync connectors) → matching files."""
    with session_scope() as s:
        rows = list(
            s.execute(select(File).where(File.source_url == url)).scalars()
        )
        if not rows:
            # Fallback: prefix match (handles fragment-bearing URLs).
            rows = list(
                s.execute(
                    select(File).where(
                        File.source_url.is_not(None),
                        File.source_url == url.split("#", 1)[0],
                    )
                ).scalars()
            )
        return [
            FileInfo(
                id=f.id,
                folder_id=f.folder_id,
                rel_path=f.rel_path,
                state=f.state,
                source_url=f.source_url,
                last_indexed_at=f.last_indexed_at,
            )
            for f in rows
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk_from_hit(h: SearchHit) -> ChunkInfo:
    p = h.payload
    return ChunkInfo(
        chunk_id=int(p.get("chunk_id", h.id)),
        file_id=int(p["file_id"]),
        file_path=str(p.get("file_path", "")),
        chunk_index=int(p.get("chunk_index", 0)),
        text=str(p.get("text", "")),
        nearby_image_ids=list(p.get("nearby_image_ids") or []),
        score=h.score,
        page=p.get("page"),
        pages=list(p.get("pages") or []),
        layout=_layout_from_payload(p),
    )


def _image_from_hit(h: SearchHit) -> ImageInfo:
    """Build an ImageInfo from a Qdrant search hit. ``width`` / ``height``
    / ``mime`` are deliberately not populated: they live on the Image DB
    row, not in the Qdrant payload, and the search path doesn't read the
    DB. Callers that need them resolve via ``get_chunk_images`` /
    ``list_page_images`` — both of which read the DB and populate."""
    p = h.payload
    return ImageInfo(
        image_id=int(p.get("image_id", h.id)),
        file_id=int((p.get("file_ids") or [0])[0]),
        file_path=str(p.get("file_path", "")),
        image_cas_id=str(p.get("image_cas_id", "")),
        page=p.get("page"),
        score=h.score,
        layout=_layout_from_payload(p),
    )


def _layout_from_payload(payload: dict) -> dict | None:
    """Re-collect the flat ``layout_*`` fields from a Qdrant payload back
    into a single dict for the LLM-facing schema.

    The indexer flattens ``layout_summary`` into top-level payload keys
    (one Qdrant payload index per scalar — see vector_store._chunk_payload)
    so each is independently filterable. The LLM doesn't filter, it
    reads, so a wrapped dict keeps the response surface tidy. Returns
    ``None`` when no layout fields are present (chunk indexed before
    the layout pipeline shipped, or non-PDF parser).
    """
    layout = {k: v for k, v in payload.items() if k.startswith("layout_")}
    return layout or None


def _nearby_image_ids(session, chunk_id: int) -> list[int]:
    return [
        link.image_id
        for link in session.execute(
            select(ChunkImageLink).where(ChunkImageLink.chunk_id == chunk_id)
        )
        .scalars()
        .all()
    ]


@lru_cache(maxsize=64)
def _load_layout_blocks(file_cas_id: str) -> tuple[dict, ...]:
    """Read + parse a file's stored ``page_layout.json`` once per CAS sha.

    LRU-cached so a sweep over a 150-page document costs one JSON parse
    total. Tuple-of-dicts (immutable container) is what makes the cache
    safe to share across threads — callers index into it but don't
    mutate. Returns ``()`` when the file has no layout (older index, or
    a non-PDF parser).
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


def _layout_for_page(file_cas_id: str, page: int) -> list[dict]:
    """Filter the cached layout list down to blocks on ``page``."""
    return [b for b in _load_layout_blocks(file_cas_id) if b.get("page") == page]


def _resize_for_response(
    data: bytes, mime: str, max_size: int
) -> tuple[bytes, str]:
    """Downscale ``data`` so its long edge is at most ``max_size`` px.

    No-op fast paths:
      * ``max_size <= 0`` — caller wants the raw blob.
      * source already fits — return original bytes/mime unchanged.

    Otherwise: decode, LANCZOS-resize preserving aspect ratio, re-encode
    as WebP at quality 75 (matches the storage default for page renders
    so the format change is invisible for that case; figures get a small
    quality hit in exchange for a much smaller payload). Decode failures
    fall back to the original bytes — better to over-deliver pixels than
    drop the response.
    """
    if max_size <= 0:
        return data, mime
    try:
        import io

        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as img:
            long_edge = max(img.width, img.height)
            if long_edge <= max_size:
                return data, mime
            scale = max_size / long_edge
            new_size = (
                max(1, int(round(img.width * scale))),
                max(1, int(round(img.height * scale))),
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


# ---------------------------------------------------------------------------
# Middleware: resolve the caller via Authorization: Bearer vk_… and stash
# their email on the ContextVar before any tool runs.
# ---------------------------------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require a valid API-key bearer token on every MCP request.

    Bypassed when ``VOITTA_SINGLE_USER`` or ``VOITTA_DEV_USER`` is set — the
    server is in local-dev mode and we trust whatever the env says. In
    production, neither is set, so every request must carry a known token.
    """

    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        if s.single_user or s.dev_user:
            # Local-dev modes: identity comes from env, not from the wire.
            ctx_token = _current_user.set(None)
            try:
                return await call_next(request)
            finally:
                _current_user.reset(ctx_token)

        bearer = _extract_bearer(request)
        if not bearer:
            return _unauthorized("Missing Authorization: Bearer token")

        # Imported lazily to avoid a circular import at module load time
        # (auth routes depend on db.models which may not yet be ready).
        from .api.routes.auth import verify_token

        with session_scope() as db:
            key = verify_token(db, bearer)
            if key is None:
                return _unauthorized("Invalid or revoked API key")
            # Materialise the email while the row is still attached.
            from .db.models import User as _User

            user = db.get(_User, key.user_id)
            email = user.email if user else None
            db.commit()

        if not email:
            # Defensive: orphaned key (user row gone). Treat as 401.
            return _unauthorized("Token does not map to a known user")

        ctx_token = _current_user.set(email)
        try:
            return await call_next(request)
        finally:
            _current_user.reset(ctx_token)


def _extract_bearer(request: Request) -> str | None:
    raw = request.headers.get("authorization") or request.headers.get("Authorization")
    if not raw:
        return None
    # RFC 6750 — case-insensitive scheme, single space separator.
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="voitta-rag-enterprise"'},
    )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def build_app(transport: str = "streamable-http", path: str | None = None):
    """Return the ASGI app exposing the MCP server.

    ``path`` controls the *internal* route the MCP transport binds to. The
    standalone runner leaves it at the default (``/mcp``); the unified app
    in ``main.py`` sets it to ``"/"`` and then mounts the whole sub-app at
    ``/mcp``.
    """
    init_db()
    app = mcp.http_app(transport=transport, stateless_http=True, path=path)
    app.add_middleware(BearerAuthMiddleware)
    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    uvicorn.run(app, host="0.0.0.0", port=settings.mcp_port)


if __name__ == "__main__":
    run()
