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
import re
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
from .services.file_classify import source_kind as classify_source_kind
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


class EntryInfo(BaseModel):
    """A single entry returned when ``list_indexed_folders`` is called with a
    non-empty ``prefix`` — i.e. when the tool is being used as a directory
    listing rather than a roots listing."""

    # ``"folder"`` for a (sub)directory, ``"file"`` for an indexed file.
    kind: str
    # Last path segment (the filename or subdir name).
    name: str
    # Full virtual path from the storage root, e.g. ``"MyDocs/sub/file.pdf"``.
    # Folder entries end with ``/``.
    path: str
    # Containing top-level folder id, so callers can pivot to other tools
    # (search with folder_ids=[…], get_file, …) without a second lookup.
    folder_id: int
    # File-only fields — None for folder entries.
    file_id: int | None = None
    state: str | None = None
    size_bytes: int | None = None
    source_url: str | None = None
    source_kind: str | None = None


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
    # Coarse classifier for an LLM that wants to branch on what kind of
    # source this is — ``"google_doc"`` / ``"google_sheet"`` /
    # ``"google_slides"`` / ``"google_form"`` / ``"google_drawing"`` for
    # Drive exports (classified by source_url), and ``"pdf"`` / ``"docx"``
    # / ``"pptx"`` / ``"xlsx"`` / ``"ipynb"`` / ``"markdown"`` / ``"text"``
    # / ``"html"`` / ``"image"`` / ``"other"`` for everything else
    # (classified by extension). See ``services/file_classify.py`` for
    # the full table.
    source_kind: str = "other"


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
    # File-level provenance, mirrored onto every chunk hit so an LLM can
    # deep-link to the canonical source (Google doc URL, GitHub raw URL,
    # …) without a follow-up ``get_file`` call. ``source_kind`` is the
    # same machine classifier carried on ``FileInfo``.
    source_url: str | None = None
    source_kind: str = "other"


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
    # File-level provenance — see ChunkInfo for the rationale.
    source_url: str | None = None
    source_kind: str = "other"


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
    # File-level provenance — see ChunkInfo.
    source_url: str | None = None
    source_kind: str = "other"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_indexed_folders(
    prefix: str | None = None,
) -> list[FolderInfo] | list[EntryInfo]:
    """Browse indexed storage as a virtual filesystem.

    Two modes, selected by ``prefix``:

    * **Roots listing** (``prefix`` is ``None`` or ``""`` or ``"/"``) — returns
      every top-level folder visible to the calling user (owned, granted,
      shared) as ``FolderInfo`` rows, with a file-count breakdown. The
      ``active`` flag tells the caller which ones are currently in their MCP
      search rotation — folders remain listed even when toggled off so the
      LLM can suggest re-enabling.

    * **Directory listing** (``prefix`` non-empty, e.g. ``"MyDocs/reports"``
      or ``"/MyDocs/reports/"``) — treats storage as a filesystem. The first
      path segment is the folder ``display_name``; the remainder is the
      rel-path inside that folder. Returns ``EntryInfo`` rows for every
      direct child: subdirectories first (``kind="folder"``), then files
      (``kind="file"``). Pass an empty string or just the folder name to
      list its root.

    Filtering: ``.voitta.meta`` sidecars and any dot-prefixed entries
    (``.git/…``, ``.DS_Store``, …) are hidden in directory mode — they are
    internal/system files and should not be surfaced to the LLM.

    :param prefix: virtual path to list. ``None`` / empty → roots.
    """
    settings = get_settings()
    user_id = _resolved_user_id()
    show_all = settings.single_user
    cleaned = (prefix or "").strip().strip("/")
    with session_scope() as s:
        if show_all or user_id is None:
            visible_ids: set[int] = {
                f.id for f in s.execute(select(Folder)).scalars()
            }
            active_ids = visible_ids
        else:
            visible_ids = set(visible_folder_ids(s, user_id))
            active_ids = set(mcp_visible_folder_ids(s, user_id))

        if not cleaned:
            out: list[FolderInfo] = []
            for f in s.execute(select(Folder).order_by(Folder.id)).scalars():
                if f.id not in visible_ids:
                    continue
                total = (
                    s.execute(
                        select(File).where(
                            File.folder_id == f.id, File.state != "deleted"
                        )
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

        head, _, tail = cleaned.partition("/")
        # Resolve the top-level segment against visible folders by
        # display_name. Falls back to numeric id for unambiguous addressing
        # when two folders share a display_name.
        candidates = [
            f
            for f in s.execute(select(Folder).order_by(Folder.id)).scalars()
            if f.id in visible_ids and f.display_name == head
        ]
        if not candidates and head.isdigit():
            f = s.get(Folder, int(head))
            if f is not None and f.id in visible_ids:
                candidates = [f]
        if not candidates:
            return []
        folder = candidates[0]

        sub_prefix = tail  # rel-path within the folder; "" means root
        like_pat = f"{sub_prefix}/%" if sub_prefix else "%"

        rows = (
            s.execute(
                select(File).where(
                    File.folder_id == folder.id,
                    File.state != "deleted",
                    File.rel_path.like(like_pat),
                    ~File.rel_path.like("%.voitta.meta"),
                )
            )
            .scalars()
            .all()
        )

        dirs: dict[str, None] = {}
        files: list[File] = []
        depth = len(sub_prefix.split("/")) if sub_prefix else 0
        for r in rows:
            parts = r.rel_path.split("/")
            if sub_prefix and parts[:depth] != sub_prefix.split("/"):
                continue
            remainder = parts[depth:]
            if not remainder:
                continue
            head_seg = remainder[0]
            if head_seg.startswith("."):
                continue
            if len(remainder) == 1:
                if any(p.startswith(".") for p in parts):
                    continue
                files.append(r)
            else:
                if any(p.startswith(".") for p in parts[:depth + 1]):
                    continue
                dirs.setdefault(head_seg, None)

        base = f"{folder.display_name}/{sub_prefix}".rstrip("/")
        entries: list[EntryInfo] = []
        for name in sorted(dirs.keys()):
            entries.append(
                EntryInfo(
                    kind="folder",
                    name=name,
                    path=f"{base}/{name}/",
                    folder_id=folder.id,
                )
            )
        for f in sorted(files, key=lambda x: x.rel_path):
            name = f.rel_path.split("/")[-1]
            entries.append(
                EntryInfo(
                    kind="file",
                    name=name,
                    path=f"{base}/{name}",
                    folder_id=folder.id,
                    file_id=f.id,
                    state=f.state,
                    size_bytes=f.size_bytes,
                    source_url=f.source_url,
                    source_kind=classify_source_kind(f),
                )
            )
        return entries


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
        return [_chunk_from_hit(h, session=s) for h in hits]


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
        return [_image_from_hit(h, session=s) for h in hits]


@mcp.tool()
def get_file(file_id: int) -> dict:
    """Return file metadata + the full extracted **markdown** content
    INLINE in the response body. Use sparingly.

    ⚠ **Prefer Python-side processing over inlining.** ``text`` is
    the Voitta-flavoured markdown extract (pypdf / python-docx /
    openpyxl / MinerU at index time, depending on format), and even
    a modest report runs to tens or hundreds of KB. Streaming all
    that into the LLM's context window for tasks that don't need
    the whole document is wasteful and slow.

    Recommended decision tree:

    * Need the **original file** (PDF/DOCX/XLSX/etc.) for parsing,
      data extraction, table analysis, summarisation, anything
      that benefits from a Python script: chain
      ``request_asset(file_id, "original")`` →
      ``fetch_to_python_storage(url=…)`` → ``run_compute(...)``.
      The bytes never enter context; the LLM works against a
      ``python_storage`` handle.
    * Need a **small, targeted span** of the markdown extract for
      reasoning: use ``get_chunk_range(file_id, start, end)`` to
      pull a few chunks instead of the whole file.
    * Need the **whole markdown extract as reading context** for a
      short document the model has to summarise / quote
      verbatim: ``get_file`` is fine.

    Rule of thumb: if you'd open the file in pandas / openpyxl /
    pypdf to answer the question, route through python_storage.
    If you'd skim it as prose to write a summary, ``get_file`` /
    ``get_chunk_range`` are the right call.
    """
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None:
            raise ValueError(f"File {file_id} not found")
        info = _file_info(f)
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

    The bounded-slice complement to ``get_file``. Returns the
    extracted markdown for a contiguous chunk range plus per-chunk
    ``page`` / ``pages`` / ``layout`` so the LLM can navigate
    "show me chunks on pages with tables" without re-running search.

    **Prefer this over ``get_file`` for targeted reads.** A search hit
    plus ±N neighbouring chunks is almost always cheaper context than
    inlining the entire markdown.

    Capped at 500 chunks per call; if you find yourself paging across
    a whole file, that's a signal to switch to
    ``request_asset(file_id, "original")`` →
    ``fetch_to_python_storage`` → ``run_compute`` instead, so the
    Python script can iterate the file without round-tripping the
    bytes through context.
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
        f_source_url = f.source_url
        f_source_kind = classify_source_kind(f)
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
                source_url=f_source_url,
                source_kind=f_source_kind,
            )
        )
    return out


@mcp.tool()
def get_chunk_images(chunk_id: int) -> list[ImageInfo]:
    """Return the figure images linked to ``chunk_id``.

    Three resolution paths, merged in priority order:

    1. **Intra-file links** (PDF/DOCX/PPTX): the indexer creates
       ``ChunkImageLink`` rows for every Image whose ``anchor_chunk`` is
       within ``nearby_radius`` of this chunk. Same-file figures only.

    2. **Cross-file markdown refs** (Google Workspace exports): the
       Drive connector writes a slide thumbnail as a *sibling* File
       row (``Pitch Deck/images/slide_12.png``) referenced from the
       slide markdown as ``![](images/slide_12.png)``. The intra-file
       linker can't see across File rows, so we re-resolve each
       markdown image reference in the chunk text against the chunk
       file's parent directory, look up the target File row, and
       return its figures. No DB writes — the resolution is cheap
       enough to do every query (one indexed lookup per reference,
       and chunks typically carry 0-1).

    Page renders are not linked — fetch them via ``list_page_images`` /
    ``get_page_image`` instead.
    """
    with session_scope() as s:
        chunk = s.get(Chunk, chunk_id)
        if chunk is None:
            raise ValueError(f"Chunk {chunk_id} not found")
        file = s.get(File, chunk.file_id)
        chunk_text = chunk.text or ""

        # (1) Intra-file links — the original path. ``score`` carries
        # the chunk-image distance so the caller can prefer the
        # tightest crops.
        intra_rows = list(
            s.execute(
                select(Image, ChunkImageLink.distance)
                .join(ChunkImageLink, ChunkImageLink.image_id == Image.id)
                .where(ChunkImageLink.chunk_id == chunk_id)
                .order_by(ChunkImageLink.distance)
            )
        )
        out: list[ImageInfo] = []
        seen_image_ids: set[int] = set()
        intra_url, intra_kind = _file_provenance(s, chunk.file_id)
        for img, distance in intra_rows:
            seen_image_ids.add(img.id)
            out.append(
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
                    source_url=intra_url,
                    source_kind=intra_kind,
                )
            )

        # (2) Cross-file markdown image references within this chunk's
        # text. Only runs when the chunk actually contains a
        # ``![...](...)`` token — cheap probe avoids work on the common
        # PDF/DOCX path.
        if file is not None and "![" in chunk_text:
            for img, target in _resolve_image_refs(s, file, chunk_text):
                if img.id in seen_image_ids:
                    continue
                seen_image_ids.add(img.id)
                out.append(_image_info_for_ref(s, img, target, score=0.0))

        # (3) File-level fallback. When the chunk produced nothing from
        # paths (1) and (2), surface the file's other inline images.
        # Rationale: a Google Doc with 21 chunks and 3 inline images
        # only has the ``![](...)`` line in 3 chunks; without this
        # fallback the other 18 chunks return ``[]`` even though the
        # document has figures the LLM might want to look at. Marked
        # with ``score=None`` so the caller can distinguish "directly
        # referenced by this chunk" (score=0) from "elsewhere in this
        # file" (score=None) from "intra-file linked with distance"
        # (score=float).
        if file is not None and not out:
            for img, target in _file_image_refs(s, file):
                if img.id in seen_image_ids:
                    continue
                seen_image_ids.add(img.id)
                out.append(_image_info_for_ref(s, img, target, score=None))
        return out


def _image_info_for_ref(session, img: Image, target: File, *, score: float | None) -> ImageInfo:
    """Build an ImageInfo for a cross-file referenced image.

    The image lives on ``target`` File row (a sibling of the chunk's
    file); the caller resolves provenance against ``target`` so the
    response carries the target's URL/kind, not the referencing file's.
    """
    t_url, t_kind = _file_provenance(session, target.id)
    return ImageInfo(
        image_id=img.id,
        file_id=img.file_id,
        file_path=target.rel_path,
        image_cas_id=img.image_cas_id,
        page=img.page,
        width=img.width,
        height=img.height,
        mime=img.mime,
        kind=img.kind,
        score=score,
        source_url=t_url,
        source_kind=t_kind,
    )


# Markdown image-syntax: ``![alt](path)``. We only care about the path;
# alt text is discarded. URLs (``http(s)://...``) skipped — the linker
# is for local sibling files written by the Drive connector, not for
# arbitrary remote images that the LLM can't fetch through this server.
_MD_IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _resolve_image_refs(
    session, owner_file: File, text: str
) -> list[tuple[Image, File]]:
    """Resolve every ``![alt](rel/path.png)`` in ``text`` to sibling Image rows.

    Walks markdown image references in document order, resolves each
    against ``owner_file``'s parent directory (Markdown's usual rule),
    looks up the matching File row in the same folder, and returns its
    figure-kind Image rows.

    ``text`` can be either a chunk's body (called from
    :func:`get_chunk_images`) or the file's whole markdown
    (:func:`_file_image_refs`); the function doesn't care which.

    Returns ``[]`` when no references resolve — when a reference is
    a remote URL, walks above the folder root, or names a file that
    isn't indexed yet (target File row absent, or zero figure rows).

    De-duplicated by target file_id so a doc referencing the same
    image twice yields one entry.
    """
    from pathlib import PurePosixPath

    parent = PurePosixPath(owner_file.rel_path).parent
    seen_files: set[int] = set()
    out: list[tuple[Image, File]] = []
    for raw_ref in _MD_IMAGE_REF.findall(text):
        # Skip remote URLs — only local sibling refs resolve.
        if raw_ref.startswith(("http://", "https://", "//", "data:")):
            continue
        try:
            joined = (parent / raw_ref).as_posix()
        except (ValueError, OSError):
            continue
        # Normalize ``a/./b`` and ``a/../b``; reject ``..`` escapes
        # that would walk above the folder root.
        target_rel = PurePosixPath(joined)
        parts = []
        for p in target_rel.parts:
            if p == "..":
                if not parts:
                    parts = None
                    break
                parts.pop()
            elif p in ("", "."):
                continue
            else:
                parts.append(p)
        if parts is None:
            continue
        target_rel_path = "/".join(parts)
        if not target_rel_path:
            continue
        target = session.execute(
            select(File).where(
                File.folder_id == owner_file.folder_id,
                File.rel_path == target_rel_path,
            )
        ).scalar_one_or_none()
        if target is None or target.id in seen_files:
            continue
        seen_files.add(target.id)
        images = list(
            session.execute(
                select(Image)
                .where(Image.file_id == target.id, Image.kind == "figure")
                .order_by(Image.image_index)
            ).scalars()
        )
        for img in images:
            out.append((img, target))
    return out


@lru_cache(maxsize=128)
def _read_cas_text(cas_id: str) -> str:
    """Cached read of a file's stored markdown.

    Multiple chunk-image lookups against the same Workspace document
    re-read the same CAS blob; cache by sha. Returns empty string for
    a missing blob — the resolver just produces no results.
    """
    try:
        raw = cas_store.read_file_blob(cas_id, "text.md")
    except FileNotFoundError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _file_image_refs(session, file: File) -> list[tuple[Image, File]]:
    """Resolve every image reference in ``file``'s stored markdown.

    Lifts the resolver from per-chunk to per-file: scans the file's
    full text once and returns every sibling-image reference. Used as
    a fallback when a chunk's own text has no image markdown but the
    file does (e.g. a 21-chunk Doc with 3 inline images: only 3 chunks
    carry the ``![](...)`` line, the rest get the same images via this
    file-level fallback). Also feeds ``list_page_images`` for
    Workspace files that have no PDF-style page renders.
    """
    if file.file_cas_id is None:
        return []
    text = _read_cas_text(file.file_cas_id)
    if not text:
        return []
    return _resolve_image_refs(session, file, text)


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
    """List visual representations of ``file_id``, in document order.

    Two paths, in priority order:

    1. **Per-page renders** (PDF pipeline): ``kind="page_render"`` rows
       written by the PDF parser — full-page WebPs (~1024px long edge)
       used as layout context.

    2. **Cross-file inline images** (Google Workspace exports): each
       ``![](images/<name>.png)`` reference in the file's markdown is
       resolved against the sibling File row in the same folder, and
       its figure-kind Image rows are returned in document-text order
       with a synthetic 1-indexed ``page`` (= appearance index in the
       markdown). This is how Slides thumbnails surface for an LLM
       that asks "what's the picture for slide N" — the slide's
       File row references its own ``images/slide_N.png`` sibling.

    Use the returned ``image_id`` with :func:`get_image` to fetch
    bytes. Returns ``[]`` for files that produce neither — a vanilla
    markdown file with no image refs, an unindexed file, etc.
    """
    with session_scope() as s:
        source_url, source_kind = _file_provenance(s, file_id)
        # (1) PDF page-renders path
        rows = list(
            s.execute(
                select(Image)
                .where(Image.file_id == file_id, Image.kind == "page_render")
                .order_by(Image.page, Image.image_index)
            ).scalars()
        )
        if rows:
            return [
                PageImageInfo(
                    image_id=img.id,
                    file_id=img.file_id,
                    page=img.page or 0,
                    width=img.width,
                    height=img.height,
                    mime=img.mime,
                    source_url=source_url,
                    source_kind=source_kind,
                )
                for img in rows
            ]

        # (2) Cross-file referenced images. ``page`` is synthetic: it's
        # the 1-indexed appearance order in the markdown, not a real
        # paginator output. Workspace files don't have a page concept
        # anyway, and the order is stable across re-renders because the
        # Drive connector writes refs in slide/section order.
        f = s.get(File, file_id)
        if f is None:
            return []
        out: list[PageImageInfo] = []
        for i, (img, target) in enumerate(_file_image_refs(s, f), start=1):
            t_url, t_kind = _file_provenance(s, target.id)
            out.append(
                PageImageInfo(
                    image_id=img.id,
                    file_id=img.file_id,
                    page=i,
                    width=img.width,
                    height=img.height,
                    mime=img.mime,
                    source_url=t_url,
                    source_kind=t_kind,
                )
            )
        return out


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
    summary as base64-encoded bytes INLINE.

    ⚠ **Almost always the wrong tool — prefer the Python path.** A
    serialised xlsx in the LLM's context is rarely useful: the model
    can't parse base64 spreadsheet bytes any better than a human.
    Almost every "what does the data say" question is better answered
    by routing the workbook through ``python_storage`` and reading
    it with pandas:

        # Recommended:
        url  = request_asset(file_id, "original")["urls"]["file"]
        snap = fetch_to_python_storage(url=url, name="<sheet>.xlsx")
        run_compute(code=f'''
            import pandas as pd
            rec = ctx.snapshot({snap["handle"]!r})
            xlsx = rec["path"] + "/" + rec["meta"]["stored_name"]
            sheets = pd.read_excel(xlsx, sheet_name=None)
            ctx.text(sheets["Q4 Plan"].head(20).to_markdown())
        ''')

    ``get_workbook`` exists only as the legacy escape hatch from
    before ``request_asset(asset_type='original')`` existed. The
    per-sheet markdown summaries cap at 100 rows so the existing
    flow ``search`` → ``get_chunk_range`` covers reasoning. For
    full-grid analysis, use the Python path.

    ``file_id`` is the id of any per-sheet ``.md`` file produced
    by the SpreadsheetExporter. Raises if the file isn't a
    Sheets-derived markdown or the xlsx isn't on disk.
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
        return [_file_info(f) for f in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_info(file: File) -> FileInfo:
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


def _file_provenance(session, file_id: int | None) -> tuple[str | None, str]:
    """Fetch ``(source_url, source_kind)`` for ``file_id`` in one query.

    Used by chunk/image hit builders to stamp file-level provenance
    onto every wire row. Returns ``(None, "other")`` when the file row
    is missing (e.g. stale Qdrant payload after a file deletion that
    Qdrant hasn't propagated yet).
    """
    if file_id is None:
        return (None, "other")
    file = session.get(File, file_id)
    if file is None:
        return (None, "other")
    return (file.source_url, classify_source_kind(file))


def _chunk_from_hit(h: SearchHit, session=None) -> ChunkInfo:
    p = h.payload
    file_id = int(p["file_id"])
    source_url, source_kind = (None, "other")
    if session is not None:
        source_url, source_kind = _file_provenance(session, file_id)
    return ChunkInfo(
        chunk_id=int(p.get("chunk_id", h.id)),
        file_id=file_id,
        file_path=str(p.get("file_path", "")),
        chunk_index=int(p.get("chunk_index", 0)),
        text=str(p.get("text", "")),
        nearby_image_ids=list(p.get("nearby_image_ids") or []),
        score=h.score,
        page=p.get("page"),
        pages=list(p.get("pages") or []),
        layout=_layout_from_payload(p),
        source_url=source_url,
        source_kind=source_kind,
    )


def _image_from_hit(h: SearchHit, session=None) -> ImageInfo:
    """Build an ImageInfo from a Qdrant search hit. ``width`` / ``height``
    / ``mime`` are deliberately not populated: they live on the Image DB
    row, not in the Qdrant payload, and the search path doesn't read the
    DB. Callers that need them resolve via ``get_chunk_images`` /
    ``list_page_images`` — both of which read the DB and populate."""
    p = h.payload
    file_id = int((p.get("file_ids") or [0])[0])
    source_url, source_kind = (None, "other")
    if session is not None:
        source_url, source_kind = _file_provenance(session, file_id)
    return ImageInfo(
        image_id=int(p.get("image_id", h.id)),
        file_id=file_id,
        file_path=str(p.get("file_path", "")),
        image_cas_id=str(p.get("image_cas_id", "")),
        page=p.get("page"),
        score=h.score,
        layout=_layout_from_payload(p),
        source_url=source_url,
        source_kind=source_kind,
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
# On-demand assets — universal channel for derived views of files.
# ---------------------------------------------------------------------------


@mcp.tool()
def list_assets(file_id: int) -> list[dict]:
    """List the on-demand assets a file exposes.

    Two sources feed this list:

    * **Synthetic, always present** — ``asset_type="original"``: serve
      the file's source bytes (PDF/DOCX/XLSX/STEP/…) via a signed URL.
      This is the escape hatch when a caller needs the raw format and
      not the Voitta markdown extract that :func:`get_file` returns.
      Use it when you want to load a file into a downstream Python
      pipeline (pandas, openpyxl, pypdf, custom parser).

    * **Parser-declared** — CAD component projections, xlsx chart
      renders, data queries, page re-renders. The on-disk
      ``on_demand_assets.json`` (written by parsers at index time)
      enumerates these. Each entry names a ``slug`` (within-file
      target — component name, sheet name) and a ``params_schema``
      fragment so the LLM can construct valid ``params``.

    Returns at least the synthetic ``original`` entry for every
    indexed file; empty list only when the file isn't yet indexed.
    """
    from .services import asset_handlers as _ah
    from .services import original_file as _orig

    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None:
            raise ValueError(f"File {file_id} not found")
        cas_id = f.file_cas_id
        rel_path = f.rel_path
    out: list[dict] = []
    # Synthetic "original" spec first — common case for the LLM is
    # "give me the bytes", so listing it at the top reduces the
    # need to scroll past parser-specific entries.
    out.append(_orig.spec_for(file_id, rel_path).as_dict())
    out.extend(spec.as_dict() for spec in _ah.load_assets_for_file(cas_id))
    return out


@mcp.tool()
def request_asset(
    file_id: int,
    asset_type: str,
    slug: str | None = None,
    params: dict | None = None,
) -> dict:
    """Request an on-demand derived view of a file.

    Two response shapes — exactly one is populated:

    * ``inline``: structured data the LLM consumes directly (rows from
      a query, summary statistics). No URL involved.
    * ``urls``: variant name → signed URL. URLs are HMAC-signed,
      short-lived (~1 hour TTL by default); the URL itself is the
      credential, no headers needed. ``expires_at`` accompanies.

    **For URLs: chain with ``fetch_to_python_storage``.** The signed
    URL doesn't reach the LLM's reasoning context — the LLM passes it
    straight to ``fetch_to_python_storage(url=..., name=...)`` to
    pull the bytes into ``python_storage``, then processes the
    resulting handle with ``run_compute``. The bytes themselves
    never enter context.

    Canonical "give me the source file" pattern (3 calls):

        asset = request_asset(file_id=N, asset_type="original")
        snap  = fetch_to_python_storage(
                    url=asset["urls"]["file"],
                    name="<filename from get_file/search>",
                )
        run_compute(code=f"rec = ctx.snapshot({snap['handle']!r}); ...")

    Common ``asset_type`` values
    ----------------------------

    * ``"original"`` — file's **source bytes** (PDF / DOCX / XLSX /
      STEP / image / whatever was indexed). Single URL under
      ``urls["file"]``. No ``slug`` or ``params``. Available on
      every indexed file. Use this anytime you want to *process*
      the file rather than read its markdown extract.

    * ``"cad_projection"`` — render four PNG views (front / top /
      side / iso) of a STEP/FCStd subcomponent. Requires ``slug``
      naming the component. Optional ``params={"size": 320}``.

    * Future / parser-specific: ``list_assets(file_id)`` is the
      source of truth for what's available for a given file.

    Use :func:`list_assets` first to discover ``asset_type`` values
    and their ``params_schema``. Invalid params raise ``ValueError``
    with the offending field; unknown ``asset_type`` raises too.
    """
    from .services import asset_handlers as _ah

    try:
        handler = _ah.get_handler(asset_type)
    except KeyError as e:
        raise ValueError(f"unknown asset_type: {asset_type!r}") from e

    raw_params = dict(params or {})
    # Hand off to the handler's own validation. Handlers raise
    # ValueError on bad input; that surfaces to the LLM verbatim.
    validated = handler.validate_params(raw_params)

    user_id = _resolved_user_id()
    response = handler.request(
        file_id=file_id,
        slug=slug,
        params=validated,
        user_id=user_id,
    )
    out: dict = {"asset_type": response.asset_type}
    if response.inline is not None:
        out["inline"] = response.inline
    if response.urls is not None:
        out["urls"] = response.urls
        if response.expires_at is not None:
            out["expires_at"] = response.expires_at
    return out


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
    # Side-effect imports: register asset_handlers before any
    # request_asset call lands. The unified app in main.py also
    # imports these (for the HTTP /api/assets/{token} route);
    # registering twice is idempotent (asset_handlers.register
    # short-circuits when the same instance re-registers).
    from .services import cad_render  # noqa: F401
    from .services import original_file  # noqa: F401

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
