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
import logging
from contextvars import ContextVar

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
    source_url: str | None
    last_indexed_at: int | None


class ChunkInfo(BaseModel):
    chunk_id: int
    file_id: int
    file_path: str
    chunk_index: int
    text: str
    nearby_image_ids: list[int] = Field(default_factory=list)
    score: float | None = None


class ImageInfo(BaseModel):
    image_id: int
    file_id: int
    file_path: str
    image_cas_id: str
    page: int | None
    width: int | None
    height: int | None
    mime: str | None
    score: float | None = None


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
    """Return chunks ``[start_index, end_index)`` of a file, in order."""
    start_index = max(0, start_index)
    end_index = min(end_index, start_index + 500)
    if end_index <= start_index:
        return []
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None:
            raise ValueError(f"File {file_id} not found")
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
        return [
            ChunkInfo(
                chunk_id=c.id,
                file_id=c.file_id,
                file_path=f.rel_path,
                chunk_index=c.chunk_index,
                text=c.text,
                nearby_image_ids=_nearby_image_ids(s, c.id),
            )
            for c in rows
        ]


@mcp.tool()
def get_chunk_images(chunk_id: int) -> list[ImageInfo]:
    """Return the images linked to ``chunk_id`` (within the configured radius)."""
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
                score=float(distance),
            )
            for (img, distance) in rows
        ]


@mcp.tool()
def get_image(image_id: int) -> dict:
    """Return image bytes (base64) + mime for inline rendering by an MCP client."""
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
    return {
        "image_id": image_id,
        "mime": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
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
    )


def _image_from_hit(h: SearchHit) -> ImageInfo:
    p = h.payload
    return ImageInfo(
        image_id=int(p.get("image_id", h.id)),
        file_id=int((p.get("file_ids") or [0])[0]),
        file_path=str(p.get("file_path", "")),
        image_cas_id=str(p.get("image_cas_id", "")),
        page=p.get("page"),
        width=None,
        height=None,
        mime=None,
        score=h.score,
    )


def _nearby_image_ids(session, chunk_id: int) -> list[int]:
    return [
        link.image_id
        for link in session.execute(
            select(ChunkImageLink).where(ChunkImageLink.chunk_id == chunk_id)
        )
        .scalars()
        .all()
    ]


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


# Backwards-compat alias for any external importer pinned to the old name.
UserHeaderMiddleware = BearerAuthMiddleware


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
