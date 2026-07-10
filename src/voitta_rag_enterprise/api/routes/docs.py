"""Authenticated API docs — /api/docs (Swagger UI) + /api/openapi.json.

The root FastAPI docs are disabled (main.py) because they'd serve the full
schema unauthenticated. These replacements require a signed-in session OR
an API key — the same ``current_user`` gate as the rest of the surface, so
"who can see the docs" always equals "who can call the API".

Both routes are ``include_in_schema=False``: the docs describe the data
API, not themselves.

Known limitation: ``get_swagger_ui_html`` loads the Swagger UI bundle from
the jsdelivr CDN — an offline/desktop deployment renders a blank page.
The schema itself (/api/openapi.json) has no such dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from ...services.acl import CurrentUser
from ..deps import current_user

router = APIRouter(tags=["docs"])


@router.get("/openapi.json", include_in_schema=False)
def openapi_json(
    request: Request,
    user: CurrentUser = Depends(current_user),
) -> JSONResponse:
    """The OpenAPI schema — works even though ``openapi_url`` is disabled
    on the app (the generator is independent of the public route)."""
    return JSONResponse(request.app.openapi())


@router.get("/docs", include_in_schema=False)
def swagger_ui(
    user: CurrentUser = Depends(current_user),
) -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="Voitta RAG Enterprise API",
    )
