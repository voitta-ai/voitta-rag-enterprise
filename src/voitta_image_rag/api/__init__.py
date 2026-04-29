"""HTTP API routers — aggregated and mounted under ``/api``."""

from fastapi import APIRouter

from .routes.files import router as files_router
from .routes.folders import router as folders_router
from .routes.images import router as images_router
from .routes.jobs import router as jobs_router
from .routes.search import router as search_router
from .routes.users import router as users_router
from .ws import router as ws_router

api_router = APIRouter()
api_router.include_router(folders_router)
api_router.include_router(files_router)
api_router.include_router(images_router)
api_router.include_router(jobs_router)
api_router.include_router(search_router)
api_router.include_router(users_router)

__all__ = ["api_router", "ws_router"]
