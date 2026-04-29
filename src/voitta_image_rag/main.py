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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("voitta-image-rag starting (data_dir=%s)", settings.data_dir)
    init_db()
    events.install_loop(asyncio.get_running_loop())
    _seed_users()
    _startup_scan()

    if not settings.disable_background:
        from .services.indexing import HANDLERS as INDEXING_HANDLERS
        from .services.watcher import (
            from_settings_for_all_folders,
            install_default,
        )
        from .services.worker import DEFAULT_HANDLERS, WorkerPool

        watcher = from_settings_for_all_folders()
        watcher.start()
        install_default(watcher)
        handlers = {**DEFAULT_HANDLERS, **INDEXING_HANDLERS}
        workers = WorkerPool(size=settings.resolved_workers(), handlers=handlers)
        await workers.start()
        app.state.watcher = watcher
        app.state.workers = workers

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


def create_app() -> FastAPI:
    app = FastAPI(title="voitta-image-rag", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def root() -> object:
            from fastapi.responses import FileResponse

            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
