"""FastAPI app factory and lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .api import api_router, ws_router
from .config import get_settings
from .db.database import init_db, session_scope
from .db.models import Folder
from .logging_config import setup_logging
from .services import events
from .services.scanner import scan_folder

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "static"


def _startup_scan() -> None:
    with session_scope() as s:
        folders = s.execute(select(Folder).where(Folder.enabled.is_(True))).scalars().all()
        for f in folders:
            r = scan_folder(s, f)
            logger.info("scan %s: +%d ~%d -%d", f.path, r.added, r.updated, r.vanished)


async def _warmup_embedders(settings: object) -> None:
    """Force every real embedder model to load before any worker runs.

    Skips when running with fake embedders (tests). Each load happens
    under gpu_lock (see embedding/{text,image}.py); calling them in
    sequence here means the lock is taken three times in series, no
    contention with anything else (workers haven't started yet).
    """
    if getattr(settings, "use_fake_embedders", False):
        return
    from .services.embedding import (
        get_image_embedder,
        get_sparse_embedder,
        get_text_embedder,
    )

    def _warm() -> None:
        # Reading .dim triggers _ensure_loaded under gpu_lock for each.
        try:
            _ = get_text_embedder().dim
        except Exception:
            logger.exception("warmup: text embedder failed")
        try:
            _ = get_image_embedder().dim
        except Exception:
            logger.exception("warmup: image embedder failed")
        try:
            get_sparse_embedder()  # bm25 has no .dim; load via factory
        except Exception:
            logger.exception("warmup: sparse embedder failed")

    await asyncio.to_thread(_warm)
    logger.info("warmup: embedders loaded")


def _seed_users() -> None:
    settings = get_settings()
    if settings.single_user:
        return
    from .services.acl import seed_users_from_file

    with session_scope() as s:
        added = seed_users_from_file(s, settings.users_file)
    if added:
        logger.info("seeded %d user(s) from %s", added, settings.users_file)


def _seed_auth_providers() -> None:
    """Upsert auth-provider rows for any credentials living in .env.

    Only Google is .env-bound today (``VOITTA_GOOGLE_AUTH_CLIENT_ID/_SECRET``).
    Re-runs on every restart so a row deleted via the admin UI reappears
    while the env vars remain set — that's the documented behaviour for
    "what is in .env should always be in the list".
    """
    settings = get_settings()
    from .services.auth_providers import upsert_env_provider

    with session_scope() as s:
        upsert_env_provider(
            s,
            provider="google",
            client_id=settings.google_auth_client_id,
            client_secret=settings.google_auth_client_secret,
        )


def create_app() -> FastAPI:
    """Build the unified web + MCP app.

    The MCP server is mounted at ``/mcp`` so a single uvicorn process serves
    everything (web UI, REST, WebSocket, MCP). FastMCP exposes its own
    lifespan; we compose it with our own.
    """
    from .mcp_server import build_app as build_mcp_app
    # Side-effect import: registers the ``cad_projection`` handler with
    # ``asset_handlers``. The MCP ``request_asset`` tool and the
    # ``/api/assets/{token}`` route resolve handlers by asset_type at
    # call time, so this import has to land before the app starts
    # serving. Adding a new on-demand handler (audio waveform, xlsx
    # chart, …) is one more import line here.
    from .services import cad_render  # noqa: F401
    # Side-effect import: registers ``asset_type="original"``, which
    # exposes the file's source bytes (PDF/DOCX/XLSX/STEP/…) via a
    # signed URL. Available on every indexed file regardless of
    # parser; ``list_assets`` synthesises the menu entry. Bookmarklet-
    # style clients use this to ingest raw bytes into a downstream
    # Python pipeline instead of consuming the markdown extract.
    from .services import original_file  # noqa: F401
    # Side-effect import: registers ``asset_type="cad_mesh"``, which
    # exports a binary glTF (.glb) of a CAD file for three.js / web
    # viewers. Available on STEP / IGES / FCStd files. ``list_assets``
    # surfaces it conditionally on extension.
    from .services import cad_mesh  # noqa: F401
    # Side-effect import: registers ``asset_type="md"``, which serves
    # the parser's normalised text.md extract via a signed URL.
    # Available whenever indexing produced a markdown blob — PDF /
    # DOCX / XLSX / PPTX / ipynb / plain text / Google Workspace
    # files synced into the index. Lets the LLM pipe the extract
    # into ``fetch_to_python_storage`` → ``run_compute`` without
    # consuming tool-result context the way ``get_file`` would.
    from .services import markdown_extract  # noqa: F401

    # Bind the MCP route at /mcp internally; below we splice its routes into
    # the parent FastAPI app rather than mounting, so the URL is exactly /mcp
    # with no 307 redirect to /mcp/.
    mcp_app = build_mcp_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(settings.data_dir / "logs")
        logger.info("Voitta RAG Enterprise starting (data_dir=%s)", settings.data_dir)
        app.state.startup_status = {"phase": "init", "ready": False}
        init_db()
        events.install_loop(asyncio.get_running_loop())
        _seed_users()
        _seed_auth_providers()
        _startup_scan()

        app.state.startup_status = {"phase": "loading models", "ready": False}

        async def _finish_startup() -> None:
            # The heavy tail (Qdrant sweeps, model warmup, worker +
            # scheduler start) runs AFTER the server starts serving, so the
            # UI is reachable immediately and can show "starting up" instead
            # of a dead page for minutes. The warmup->worker order inside is
            # preserved (two CUDA contexts at once corrupts the heap).
            try:
                from .services import job_queue
                from .services.indexing import (
                    HANDLERS as INDEXING_HANDLERS,
                )
                from .services.indexing import reconcile_abandoned_extracts
                from .services.watcher import (
                    from_settings_for_all_folders,
                    install_default,
                )
                from .services.worker import DEFAULT_HANDLERS, WorkerPool

                requeued, killed = job_queue.reclaim_abandoned_jobs()
                if requeued or killed:
                    logger.warning(
                        "abandoned-jobs reconcile: requeued=%d killed=%d",
                        requeued,
                        killed,
                    )
                # Bootstrap the folder-active tracker *after* the abandoned-job
                # sweep so we don't count rows that are about to be flipped to
                # 'error'. Subsequent enqueue/finish hooks maintain it incrementally.
                from .services import folder_active

                folder_active.init_from_db()
                extracts_repaired = reconcile_abandoned_extracts()
                if extracts_repaired:
                    logger.warning(
                        "reset %d file(s) from extracted/embedding -> pending"
                        " (extract job was abandoned)",
                        extracts_repaired,
                    )

                # Qdrant orphan-point sweep. Cleans points whose payload id
                # no longer matches any SQLite row:
                #   - Images: caused by the pre-fix _commit_indexing path
                #     deleting Image DB rows but not their Qdrant points.
                #   - Chunks: shouldn't accumulate (replace_chunks_for_file
                #     is atomic), but we sweep anyway as defense-in-depth.
                # Idempotent on subsequent runs.
                try:
                    from sqlalchemy import select as _select

                    from .db.database import session_scope as _ss
                    from .db.models import Chunk as _Chunk
                    from .db.models import Image as _Image
                    from .services.vector_store import (
                        delete_orphan_chunk_points,
                        delete_orphan_image_points,
                    )

                    with _ss() as _s:
                        known_image_ids = {
                            iid for (iid,) in _s.execute(_select(_Image.id)).all()
                        }
                        known_chunk_ids = {
                            cid for (cid,) in _s.execute(_select(_Chunk.id)).all()
                        }
                    deleted_imgs = delete_orphan_image_points(known_image_ids)
                    if deleted_imgs:
                        logger.warning(
                            "deleted %d orphan image point(s) from Qdrant "
                            "(stale image_id payloads from a pre-fix re-extract)",
                            deleted_imgs,
                        )
                    deleted_chunks = delete_orphan_chunk_points(known_chunk_ids)
                    if deleted_chunks:
                        logger.warning(
                            "deleted %d orphan chunk point(s) from Qdrant "
                            "(stale chunk_id payloads)",
                            deleted_chunks,
                        )
                except Exception:  # pragma: no cover — never fail boot for this
                    logger.exception("orphan-point sweep failed at startup")

                # Index health: warn if any folder has files marked indexed in
                # SQLite but no chunk points in Qdrant (the Qdrant store was
                # wiped or moved). The user has to Reindex to repopulate; we
                # surface it on startup so they don't discover it via empty
                # search results an hour later.
                try:
                    from .db.database import session_scope as _ss
                    from .services.reconcile import log_startup_warnings

                    with _ss() as _s:
                        log_startup_warnings(_s)
                except Exception:  # pragma: no cover — never fail boot for this
                    logger.exception("index-health check failed at startup")

                watcher = from_settings_for_all_folders()
                watcher.start()
                install_default(watcher)
                handlers = {**DEFAULT_HANDLERS, **INDEXING_HANDLERS}
                # Pre-warm the embedders before any worker can claim a job.
                # Lazy-loading them on first use means the load (CUDA weight
                # transfer) can run on a request thread while the worker is
                # mid-MinerU on another thread — two CUDA contexts in flight,
                # glibc detects heap corruption ("malloc_consolidate: unaligned
                # fastbin chunk"). Pre-warming under gpu_lock at startup means
                # all later calls take the fast path.
                await _warmup_embedders(settings)

                n_workers = settings.resolved_workers()
                logger.info(
                    "starting indexer pool with %d worker%s "
                    "(serial extract is the design — set VOITTA_WORKERS to override)",
                    n_workers,
                    "" if n_workers == 1 else "s",
                )
                workers = WorkerPool(size=n_workers, handlers=handlers)
                await workers.start()
                app.state.watcher = watcher
                app.state.workers = workers

                # Auto-sync scheduler: ticks once a minute, enqueues a sync
                # job for any folder_sync_sources row whose auto_sync_hours
                # interval has lapsed since last_synced_at. Same dedup key as
                # the manual /sync/trigger endpoint, so a still-running sync
                # is coalesced.
                from .services import scheduler as auto_sync_scheduler

                app.state.scheduler_task = asyncio.create_task(
                    auto_sync_scheduler.run_forever()
                )
                app.state.startup_status = {"phase": "ready", "ready": True}
                logger.info("background startup complete — indexer ready")
            except Exception:
                logger.exception("background startup failed")
                app.state.startup_status = {"phase": "error", "ready": False}

        if settings.disable_background:
            app.state.startup_status = {"phase": "ready", "ready": True}
        else:
            app.state.bg_startup_task = asyncio.create_task(_finish_startup())

        # Run the mounted MCP app's lifespan as well.
        async with mcp_app.router.lifespan_context(mcp_app):
            try:
                yield
            finally:
                import contextlib

                # Background startup may still be warming up — stop it first so
                # it doesn't start workers/watcher mid-teardown.
                if getattr(app.state, "bg_startup_task", None) is not None:
                    app.state.bg_startup_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await app.state.bg_startup_task
                if hasattr(app.state, "scheduler_task"):
                    app.state.scheduler_task.cancel()
                    # Awaiting the cancelled task lets it run its own
                    # CancelledError handler; we swallow whatever bubbles
                    # out (CancelledError on success, anything else means
                    # the loop body raised right before cancel).
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await app.state.scheduler_task
                if hasattr(app.state, "workers"):
                    await app.state.workers.stop()
                if hasattr(app.state, "watcher"):
                    from .services.watcher import uninstall_default

                    app.state.watcher.stop()
                    uninstall_default()
                events.uninstall_loop()
                logger.info("Voitta RAG Enterprise stopped")

    app = FastAPI(title="Voitta RAG Enterprise", version="0.1.0", lifespan=lifespan)

    # Signed session cookie — used by the Google login flow to persist the
    # authenticated email across requests. Kept to ``/`` so both REST routes
    # and the SPA itself see it; ``same_site=lax`` lets the OAuth callback
    # redirect carry it on the way back from Google.
    from starlette.middleware.sessions import SessionMiddleware

    settings = get_settings()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.resolved_session_secret(),
        session_cookie="voitta_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",        # keep lax — Strict breaks the OAuth callback redirect
        https_only=settings.cookie_secure,  # adds Secure attr; disable only for http://localhost dev
    )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/health", tags=["health"])
    async def api_health() -> dict:
        """Startup readiness for the SPA. ``ready`` flips true once background
        startup (model warmup + workers) finishes; ``phase`` is a human label
        of the current step while starting. The SPA shows a 'starting up'
        banner until ready."""
        status = getattr(app.state, "startup_status", None) or {
            "phase": "ready", "ready": True
        }
        return status

    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)

    # MCP under /mcp on the same port. We splice the routes (rather than
    # ``app.mount``) so the canonical URL is /mcp with no trailing-slash
    # redirect. Middleware on the inner app does not run for spliced routes,
    # so we re-apply the bearer-auth bridge at the FastAPI level — but only
    # for ``/mcp`` paths, since the SPA's API uses session cookies.
    from starlette.middleware.base import BaseHTTPMiddleware

    from .mcp_server import BearerAuthMiddleware

    class _McpAuthBridge(BaseHTTPMiddleware):
        """Run BearerAuthMiddleware only for /mcp paths.

        We can't simply ``add_middleware(BearerAuthMiddleware)`` because that
        would also intercept the SPA / REST routes which authenticate via a
        session cookie, not a bearer.
        """

        def __init__(self, app):
            super().__init__(app)
            self._bearer = BearerAuthMiddleware(app)

        async def dispatch(self, request, call_next):
            if not request.url.path.startswith("/mcp"):
                return await call_next(request)
            return await self._bearer.dispatch(request, call_next)

    for route in mcp_app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(_McpAuthBridge)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def root() -> object:
            from fastapi.responses import FileResponse

            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> object:
            # Browsers request /favicon.ico unconditionally even when
            # the page declares a <link rel="icon"> — serve the SVG
            # so the request resolves instead of 404'ing in the
            # console.
            from fastapi.responses import FileResponse

            return FileResponse(
                STATIC_DIR / "favicon.svg", media_type="image/svg+xml"
            )

    return app


app = create_app()
