"""HTTP API routers — aggregated and mounted under ``/api``."""

from fastapi import APIRouter

from .routes.admin import router as admin_router
from .routes.auth import router as auth_router
from .routes.files import router as files_router
from .routes.folders import router as folders_router
from .routes.images import router as images_router
from .routes.jobs import router as jobs_router
from .routes.search import router as search_router
from .routes.sync import oauth_router as sync_oauth_router
from .routes.sync import router as sync_router
from .routes.users import router as users_router
from .ws import router as ws_router

api_router = APIRouter()
# Auth must mount before the per-folder/sync routes that depend on
# ``current_user``. Order doesn't actually matter for routing in FastAPI,
# but registering it first reads the right way at the call site.
api_router.include_router(auth_router)
api_router.include_router(folders_router)
api_router.include_router(files_router)
api_router.include_router(images_router)
api_router.include_router(jobs_router)
api_router.include_router(search_router)
api_router.include_router(sync_router)
api_router.include_router(sync_oauth_router)
api_router.include_router(users_router)
api_router.include_router(admin_router)

__all__ = ["api_router", "ws_router"]
