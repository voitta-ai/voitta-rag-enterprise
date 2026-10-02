"""MCP server.

Exposes the same data the HTTP API does, but as MCP tools that an LLM agent
can call. The tools are thin wrappers: retrieval logic and its folder ACL
live in ``services/retrieval`` (shared with the in-app assistant); this
module owns MCP transport, authentication and the tool descriptions.

Authentication
--------------
Clients authenticate with a personal API key minted from the SPA's Settings
panel and presented as ``Authorization: Bearer vk_…``. The middleware
resolves that token to an account, sets it on a ContextVar that every tool
reads through ``_viewer``, and bumps ``last_used_at``. A request with a
missing or invalid bearer is rejected with 401.

The ``VOITTA_SINGLE_USER`` and ``VOITTA_DEV_USER`` env modes still bypass
authentication: those are local-dev shortcuts and the bearer requirement
would only get in the way. In production neither is set, so every MCP call
must carry a valid bearer.

Run standalone::

    python -m voitta_rag_enterprise.mcp_server
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import get_settings
from .db.database import init_db, session_scope
from .services import retrieval
from .services.acl import get_or_create_user
from .services.retrieval import (
    ChunkInfo,
    EntryInfo,
    FileInfo,
    FolderInfo,
    ImageInfo,
    PageImageInfo,
    Viewer,
)

logger = logging.getLogger(__name__)

# (email, account_id) of the bearer-authenticated caller. The account id —
# ApiKey.user_id — is what scopes visibility: a key minted while an account
# was active stays scoped to that account (multi-account users hold one key
# per account, so the key IS the account selector). Email rides along for
# logging/display; resolving identity by email alone is no longer possible
# now that one email may own several account rows.
_current_user: ContextVar[tuple[str, int] | None] = ContextVar(
    "voitta_mcp_user", default=None
)

_INSTRUCTIONS = """
You are connected to Voitta RAG Enterprise — a filesystem-driven RAG index with
hybrid text search (e5-base-v2 dense + BM25 sparse, RRF-fused) and cross-modal
image search (SigLIP-2).

## Recommended workflow

1. **Orient first.** Start every session with `list_indexed_folders()` (no
   prefix). This tells you which folders exist, how many files are indexed,
   and which ones are active in the caller's MCP rotation. Folder `display_name`
   values are your primary navigation handles — read them carefully before
   formulating queries.

2. **Search broadly, then narrow.** Run `search(query, limit=20)` across all
   active folders first. Once you identify the relevant folder, re-run with
   `folder_ids=[...]` to cut noise. Keep queries short and concrete — BM25
   rewards exact token matches and the dense model handles paraphrase.

3. **Expand context efficiently.** Prefer `get_chunk_range(file_id, start, end)`
   over `get_file` for targeted reads — pull ±5 chunks around a hit rather than
   inlining the full document. Reserve `get_file` for short documents (< ~20
   chunks) where you need everything.

4. **Images.** Use `search_images` for visual queries ("bar chart showing EMEA
   growth"). Use `get_image(image_id)` to fetch bytes. Use `list_page_images` /
   `get_page_image` for full-page layout context on PDFs.

5. **Binary / structured files.** For spreadsheets, PDFs, CAD files — do not
   inline them into context. Use `request_asset(file_id, "original")` →
   `fetch_to_python_storage(url=...)` → `run_compute(code=...)` so the bytes
   stay in Python storage and never enter the LLM context window.

## Best practices to maintain

- **Folder names matter.** `display_name` is the primary signal the LLM uses to
  scope searches. Well-named folders ("Stella Google Drive", "McKinsey Quarterly
  Reports") produce better results than generic ones ("GDrive", "Docs").

- **File paths carry meaning.** The `rel_path` is embedded in every chunk
  payload. Meaningful paths ("reports/2025/Q1-revenue.pdf") give the model
  provenance context without a follow-up `get_file` call.

- **Use `source_url` for deep-links.** Chunks from Google Drive or GitHub syncs
  carry a `source_url` pointing back to the original. Always surface this link
  when citing a result — it lets the user open the canonical source directly.

- **Respect `active` flags.** `list_indexed_folders` returns an `active` field
  per folder. Folders toggled off by the user should not be searched unless
  they explicitly ask. You may mention that a relevant folder is inactive and
  offer to include it.

- **Scope image search.** `search_images` searches the SigLIP-2 collection
  separately from text chunks — run it in parallel with `search` when the query
  is likely to have a visual answer (charts, diagrams, photos, slides).

See the project README ("Best practices" section) for the full guide on folder
naming, file organisation, and search quality.
"""

mcp = FastMCP("voitta-rag-enterprise", instructions=_INSTRUCTIONS)


def _viewer() -> Viewer:
    """Resolve the calling ACCOUNT into a retrieval :class:`Viewer`.

    Priority — first match wins:

    1. ``VOITTA_SINGLE_USER`` → unrestricted (the sole identity owns all)
    2. ``VOITTA_DEV_USER`` → that email's Personal account (created on
       first call)
    3. the account id ``BearerAuthMiddleware`` put on the ContextVar — the
       verified ``ApiKey.user_id``, never an email lookup (one email may
       own several account rows)
    4. unrestricted — reachable only by direct in-process tool calls;
       network requests without a valid bearer are rejected by the
       middleware before any tool runs

    Scope is ``"mcp_active"``: the user's per-folder MCP opt-outs apply.
    """
    s = get_settings()
    if s.single_user:
        return Viewer(user_id=None)
    if s.dev_user:
        with session_scope() as db:
            return Viewer(user_id=get_or_create_user(db, s.dev_user).id)
    ctx = _current_user.get()
    return Viewer(user_id=ctx[1] if ctx is not None else None)


# ---------------------------------------------------------------------------
# Tools — thin wrappers over services.retrieval. The docstrings are the tool
# descriptions MCP clients show their LLM; the logic (and the folder ACL)
# lives in the retrieval package.
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
    return retrieval.list_indexed_folders(_viewer(), prefix)


@mcp.tool()
def search(
    query: str,
    folder_ids: list[int] | None = None,
    limit: int = 20,
) -> list[ChunkInfo]:
    """Hybrid (dense + sparse, RRF-fused) search over text chunks.

    Results may include ``kind='folder_card'`` hits: the query matched a
    folder/subfolder *name or description* rather than document content.
    A card has no file (``file_id`` is null) — treat it as a pointer and
    re-run search with ``folder_ids=[hit.folder_id]`` to drill into that
    folder's documents.

    :param query: free-text query
    :param folder_ids: restrict search to these folder ids
    :param limit: max hits (1..100)
    """
    return retrieval.search(_viewer(), query, folder_ids, limit)


@mcp.tool()
def search_images(
    query: str,
    folder_ids: list[int] | None = None,
    limit: int = 20,
) -> list[ImageInfo]:
    """Cross-modal text→image search via the image embedder's text encoder."""
    return retrieval.search_images(_viewer(), query, folder_ids, limit)


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
    return retrieval.get_file(_viewer(), file_id)


@mcp.tool()
def get_chunk_range(
    file_id: int,
    start: int = 0,
    end: int = 10,
) -> list[ChunkInfo]:
    """Return chunks ``[start, end)`` of a file, in order.

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
    return retrieval.get_chunk_range(_viewer(), file_id, start, end)


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
    return retrieval.get_chunk_images(_viewer(), chunk_id)


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
    return retrieval.get_image(_viewer(), image_id, max_size)


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
    return retrieval.list_page_images(_viewer(), file_id)


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
    return retrieval.get_page_image(_viewer(), file_id, page, max_size, include_layout)


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
    return retrieval.get_page_layout(_viewer(), file_id, page)


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
    return retrieval.get_workbook(_viewer(), file_id)


@mcp.tool()
def resolve_url(url: str) -> list[FileInfo]:
    """Reverse-lookup an external URL (set by sync connectors) → matching files."""
    return retrieval.resolve_url(_viewer(), url)


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
    return retrieval.list_assets(_viewer(), file_id)


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

    * ``"md"`` — the parser's normalised **markdown extract**, served
      as ``text/markdown`` via ``urls["md"]``. Same content
      ``get_file`` returns, but as a fetchable URL — chain into
      ``fetch_to_python_storage`` → ``run_compute`` to do bulk
      regex / dataframe / NLP work over a long DOCX/XLSX/PPTX
      without piping the text through tool-result context.
      No ``slug``, no ``params``. Available whenever indexing
      produced a ``text.md`` blob (PDF, DOCX, XLSX, PPTX, ipynb,
      plain text, and Google Workspace files synced into the
      voitta-rag-enterprise index). Use ``original`` instead when
      you need the source format (custom OCP, openpyxl, layout-
      preserving parser, …).

    * ``"cad_projection"`` — render four PNG views (front / top /
      side / iso) of a STEP/FCStd subcomponent. Requires ``slug``
      naming the component. Optional ``params={"size": 320}``.

    * ``"cad_mesh"`` — export a STEP / IGES / FCStd file as a binary
      glTF (``.glb``, mime ``model/gltf-binary``) suitable for
      ``three.js`` ``GLTFLoader`` / web viewers. Single URL under
      ``urls["mesh"]``. The scene contains one named node per
      component, so a viewer can list / hide / colour parts by
      ``node.name``. Without ``slug`` the whole assembly is
      exported; with a ``slug`` from ``cad_projection``, only that
      component. Optional ``params={"linear_deflection": 0.5}``
      controls tessellation tolerance in millimetres.

    * Future / parser-specific: ``list_assets(file_id)`` is the
      source of truth for what's available for a given file.

    Use :func:`list_assets` first to discover ``asset_type`` values
    and their ``params_schema``. Invalid params raise ``ValueError``
    with the offending field; unknown ``asset_type`` raises too.
    """
    return retrieval.request_asset(_viewer(), file_id, asset_type, slug, params)


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
        from .api.routes.api_keys import commit_best_effort, identity_for_token
        from .api.routes.company_keys import (
            USER_EMAIL_HEADER,
            is_company_bearer,
            resolve_company_identity,
        )

        if is_company_bearer(bearer):
            # Company key (cvk_…) + user email — either the dedicated
            # header or embedded as "cvk_…:email". Resolves through the
            # key's company scope (Clerk membership / native allowlist)
            # and JIT-provisions the account row.
            identity = await resolve_company_identity(
                bearer, request.headers.get(USER_EMAIL_HEADER)
            )
            if identity is None:
                return _unauthorized(
                    "Invalid company API key or user email (send "
                    "X-Voitta-User-Email or append ':email' to the token)"
                )
        else:
            with session_scope() as db:
                # (email, account_id) — the id is the ACCOUNT the key was
                # minted under; that, not the email, drives every visibility
                # filter. None covers both invalid tokens and orphaned keys.
                identity = identity_for_token(db, bearer)
                commit_best_effort(db)

            if identity is None:
                return _unauthorized("Invalid or revoked API key")

        ctx_token = _current_user.set(identity)
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
    # Side-effect imports: register asset_handlers before any
    # request_asset call lands. The unified app in main.py also
    # imports these (for the HTTP /api/assets/{token} route);
    # registering twice is idempotent (asset_handlers.register
    # short-circuits when the same instance re-registers).
    from .services import (
        cad_mesh,  # noqa: F401
        cad_render,  # noqa: F401
        markdown_extract,  # noqa: F401
        original_file,  # noqa: F401
    )

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
