"""Generic on-demand asset fetch endpoint — ``GET /api/assets/{token}``.

One route serves every asset_type. The token (issued by an ``AssetHandler``
via :func:`signed_assets.issue_token`) carries the file id, asset type,
slug, params, and the issuing user's id. We validate the signature,
re-check ACL against the *current* user's identity (impersonation /
revocation aside), then dispatch to the handler's :meth:`fetch`.

Why bother revalidating ACL when the token already carries ``uid``?
Because the bearer presenting the token might be a different session
than the one that minted it — the token is a capability, but the
current user's permissions might have changed. The cheaper check is
"current_user equals the uid that minted the token" — short of that
the request is rejected even if the signature is fine.

Renders are **not cached**: the handler does the full pipeline every
time. That's deliberate (see ``cad_render`` for the rationale — a
re-extract is the only way the index becomes the truth, so we never
serve from an aging cache).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...db.models import File
from ...services import asset_handlers, signed_assets
from ...services.acl import CurrentUser, user_can_see_folder
from ..deps import current_user, db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/{token}")
def fetch_asset(
    token: str,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> Response:
    """Validate the signed token, dispatch to the registered handler,
    return bytes. 404 / 401 / 410 surface specific failure modes so a
    misbehaving LLM caller can self-diagnose without guessing.
    """
    try:
        claims = signed_assets.verify_token(token)
    except signed_assets.InvalidAssetToken as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

    # ACL belt-and-braces: the bearer presenting this token must be the
    # same user who issued it. Tokens are short-lived (default 1 hour)
    # so this is mostly defensive — but it makes it impossible for one
    # user to share their token with another and grant cross-user read.
    if claims.user_id is not None and claims.user_id != user.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token belongs to another user")

    file = db.get(File, claims.file_id)
    if file is None:
        # Don't leak existence — but in this case the token was issued
        # for a real file, so the most likely explanation is the file
        # was deleted between issue and fetch. 410 Gone says "the
        # resource was here, it isn't now".
        raise HTTPException(status.HTTP_410_GONE, "file deleted")
    if not user_can_see_folder(db, file.folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")

    try:
        handler = asset_handlers.get_handler(claims.asset_type)
    except KeyError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown asset_type: {claims.asset_type!r}",
        ) from e

    # The handler may have stuffed a ``variant`` into params at issue
    # time (e.g. ``"front"``/``"top"`` for CAD projections — same
    # asset_type, different rendering parameters). We pull it out so
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
        # Slug/component/sheet not found in the current parse.
        # Hard error by design — the index is supposed to match the
        # file exactly. If a stale slug shows up here, the file got
        # re-extracted between LLM call and fetch.
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except TimeoutError as e:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(e)) from e

    return Response(content=rendered.body, media_type=rendered.mime)
