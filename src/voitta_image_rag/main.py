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


def _seed_users() -> None:
    settings = get_settings()
    if settings.single_user:
        return
    from .services.acl import seed_users_from_file

    with session_scope() as s:
        added = seed_users_from_file(s, settings.users_file)
    if added:
        logger.info("seeded %d user(s) from %s", added, settings.users_file)


def create_app() -> FastAPI:
    """Build the unified web + MCP app.

    The MCP server is mounted at ``/mcp`` so a single uvicorn process serves
    everything (web UI, REST, WebSocket, MCP). FastMCP exposes its own
    lifespan; we compose it with our own.
    """
    from .mcp_server import build_app as build_mcp_app

    # Bind the MCP route at /mcp internally; below we splice its routes into
    # the parent FastAPI app rather than mounting, so the URL is exactly /mcp
    # with no 307 redirect to /mcp/.
    mcp_app = build_mcp_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(settings.data_dir / "logs")
        logger.info("voitta-image-rag starting (data_dir=%s)", settings.data_dir)
        init_db()
        events.install_loop(asyncio.get_running_loop())
        _seed_users()
        _startup_scan()

        if not settings.disable_background:
            from .services.indexing import (
                HANDLERS as INDEXING_HANDLERS,
            )
            from .services.indexing import (
                reconcile_pending_embeds,
                reconcile_unsupported_files,
            )
            from .services.watcher import (
                from_settings_for_all_folders,
                install_default,
            )
            from .services.worker import DEFAULT_HANDLERS, WorkerPool

            repaired = reconcile_pending_embeds()
            if repaired:
                logger.warning(
                    "reconciled %d file(s) stuck in pending state at startup",
                    repaired,
                )
            moved = reconcile_unsupported_files()
            if moved:
                logger.info(
                    "migrated %d file(s) from error -> unsupported (no-parser)",
                    moved,
                )

            watcher = from_settings_for_all_folders()
            watcher.start()
            install_default(watcher)
            handlers = {**DEFAULT_HANDLERS, **INDEXING_HANDLERS}
            workers = WorkerPool(size=settings.resolved_workers(), handlers=handlers)
            await workers.start()
            app.state.watcher = watcher
            app.state.workers = workers

        # Run the mounted MCP app's lifespan as well.
        async with mcp_app.router.lifespan_context(mcp_app):
            try:
                yield
            finally:
                if hasattr(app.state, "workers"):
                    await app.state.workers.stop()
                if hasattr(app.state, "watcher"):
                    from .services.watcher import uninstall_default

                    app.state.watcher.stop()
                    uninstall_default()
                events.uninstall_loop()
                logger.info("voitta-image-rag stopped")

    app = FastAPI(title="voitta-image-rag", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)

    # MCP under /mcp on the same port. We splice the routes (rather than
    # ``app.mount``) so the canonical URL is /mcp with no trailing-slash
    # redirect. Middleware on the inner app does not run for spliced routes;
    # the X-User-Name → ContextVar bridge is re-applied at the FastAPI level.
    from starlette.middleware.base import BaseHTTPMiddleware

    from .mcp_server import _current_user

    class _McpUserHeader(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not request.url.path.startswith("/mcp"):
                return await call_next(request)
            token = _current_user.set(request.headers.get("X-User-Name"))
            try:
                return await call_next(request)
            finally:
                _current_user.reset(token)

    for route in mcp_app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(_McpUserHeader)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def root() -> object:
            from fastapi.responses import FileResponse

            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
