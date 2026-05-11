"""Generic on-demand asset fetch endpoint — ``GET /api/assets/{token}``.

One route serves every asset_type. The token is an HMAC-signed
capability minted by ``request_asset`` (an authenticated MCP call):
the token itself encodes file_id, asset_type, slug, and params, and
is signed with the server's secret.

**The URL is the credential.** Anyone with the URL during its TTL
(default 1 hour) gets the bytes — same pattern as S3 pre-signed URLs.
The ACL gate runs at mint time (``request_asset`` requires the
authenticated user to be able to see the file); the fetch path
trusts a valid signature.

Rationale: LLM agents are the primary consumer of these URLs and they
have no out-of-band Bearer-auth fetcher. Making the URL fetchable by
anyone holding it within the short TTL keeps the design simple for
LLM consumption AND for human/frontend consumption (browsers, curl,
WebFetch tools) without changing the security model meaningfully —
unguessable signed tokens with a short expiry are the cap.

Renders are **not cached**: the handler does the full pipeline every
time. That's deliberate (see ``cad_render`` for the rationale — a
re-extract is the only way the index becomes the truth, so we never
serve from an aging cache).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from ...services import asset_handlers, signed_assets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/{token}")
def fetch_asset(token: str) -> Response:
    """Validate the signed token, dispatch to the registered handler,
    return bytes. No user auth — the signed URL IS the credential.

    Status codes:

    * 200 — bytes served
    * 401 — token signature failed, malformed, or expired
    * 400 — token references an unregistered asset_type or bad params
    * 404 — slug not present in the file's current asset menu (stale)
    * 410 — file deleted since the URL was minted
    * 504 — handler exceeded its wall-clock budget
    """
    try:
        claims = signed_assets.verify_token(token)
    except signed_assets.InvalidAssetToken as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

    try:
        handler = asset_handlers.get_handler(claims.asset_type)
    except KeyError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown asset_type: {claims.asset_type!r}",
        ) from e

    # The handler may have stuffed a ``variant`` into params at issue
    # time (e.g. ``"front"``/``"top"`` for CAD projections — same
    # asset_type, different rendering parameters). Pull it back out so
    # the handler's :meth:`fetch` signature stays uniform.
    params = dict(claims.params or {})
    variant = params.pop("__variant__", None)

    try:
        rendered = handler.fetch(
            file_id=claims.file_id,
            slug=claims.slug,
            params=params,
            user_id=claims.user_id,
            variant=variant,
        )
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_410_GONE, str(e)) from e
    except (LookupError, KeyError) as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except TimeoutError as e:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(e)) from e

    return Response(content=rendered.body, media_type=rendered.mime)
