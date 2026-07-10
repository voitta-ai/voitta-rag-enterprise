"""Indexing caps — admin-tunable per-format / per-file limits."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

from ....services import indexing_caps
from ....services.acl import CurrentUser
from ...deps import admin_user, super_admin_user
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Indexing caps — admin-tunable per-format / per-file limits.
# ---------------------------------------------------------------------------


class IndexingCapsOut(BaseModel):
    values: dict[str, int]
    defaults: dict[str, int]
    bounds: dict[str, list[int]]


@router.get("/indexing-caps", response_model=IndexingCapsOut)
def get_indexing_caps(_: CurrentUser = Depends(admin_user)) -> IndexingCapsOut:
    """Return current cap values plus defaults + bounds for the UI.

    The values reflect the override JSON merged over the shipped defaults
    (and ``Settings``-sourced env defaults for fields that have both).
    The UI renders each row with min/max ``input`` attributes pulled from
    ``bounds`` and a "reset" button that posts the matching ``defaults``
    entry back.
    """
    return IndexingCapsOut(
        values=indexing_caps.as_dict(),
        defaults=indexing_caps.defaults_dict(),
        bounds=indexing_caps.bounds_dict(),
    )


@router.patch("/indexing-caps", response_model=IndexingCapsOut)
def update_indexing_caps(
    body: dict[str, int],
    me: CurrentUser = Depends(super_admin_user),
) -> IndexingCapsOut:
    """Merge ``body`` (partial) into the persisted override and re-cache.

    Unknown keys are dropped; out-of-bounds values are clamped to the
    declared range in :data:`indexing_caps.BOUNDS`. Non-integer values
    return 400.
    """
    try:
        indexing_caps.update(body)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s updated indexing caps: keys=%s", me.email, sorted(body))
    publish_admin_state()
    return IndexingCapsOut(
        values=indexing_caps.as_dict(),
        defaults=indexing_caps.defaults_dict(),
        bounds=indexing_caps.bounds_dict(),
    )
