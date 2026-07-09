"""Confluence sync source — schemas, PUT apply logic, and the space picker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....db.models import FolderSyncSource
from ....services.acl import CurrentUser
from ....services.sync.atlassian_auth import AtlassianAuth, normalize_base_url
from ....services.sync.confluence import (
    PAGES_UPDATED_SINCE as CF_DEFAULT_SINCE,
)
from ....services.sync.confluence import (
    coerce_spaces_field,
    encode_spaces_field,
    search_spaces,
)
from ...deps import current_user, db_session
from . import registry
from .base import check_owner, router, validate_since

if TYPE_CHECKING:
    from .core import SyncSourceIn


class ConfluenceSpace(BaseModel):
    key: str
    name: str = ""


class ConfluenceSyncIn(BaseModel):
    """Payload when ``source_type == 'confluence'``."""

    base_url: str = ""
    auth_method: Literal["cloud", "server"] = "cloud"
    email: str = ""
    token: str = ""
    spaces: list[ConfluenceSpace] = Field(default_factory=list)
    all_spaces: bool = False
    cql: str = ""
    updated_since: str = ""  # YYYY-MM-DD recency floor; "" → connector default


class ConfluenceSyncOut(BaseModel):
    base_url: str
    auth_method: str
    email: str
    spaces: list[ConfluenceSpace]
    all_spaces: bool
    cql: str
    updated_since: str
    default_since: str
    has_token: bool
    connected: bool


def clear_fields(src: FolderSyncSource) -> None:
    src.cf_base_url = None
    src.cf_auth_method = None
    src.cf_email = None
    src.cf_token = None
    src.cf_selected_spaces = None
    src.cf_all_spaces = False
    src.cf_cql = None
    src.cf_updated_since = None


def build_out(src: FolderSyncSource) -> ConfluenceSyncOut:
    method = src.cf_auth_method or "cloud"
    base = src.cf_base_url or ""
    token_set = bool(src.cf_token)
    email_set = bool(src.cf_email)
    return ConfluenceSyncOut(
        base_url=base,
        auth_method=method,
        email=src.cf_email or "",
        spaces=[
            ConfluenceSpace(**s)
            for s in coerce_spaces_field(src.cf_selected_spaces)
        ],
        all_spaces=bool(src.cf_all_spaces),
        cql=src.cf_cql or "",
        updated_since=src.cf_updated_since or "",
        default_since=CF_DEFAULT_SINCE,
        has_token=token_set,
        connected=bool(base and token_set and (method == "server" or email_set)),
    )


def apply_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
) -> FolderSyncSource:
    """Apply a ``confluence`` payload to a (new or existing) row.

    Cloud needs an email (Basic ``email:token``); server needs only the PAT.
    The token input is masked client-side, so a blank ``token`` on an existing
    row means "leave the stored one alone", not "clear it".
    """
    cfg = body.confluence
    if cfg is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing 'confluence' config")
    base_url = normalize_base_url(cfg.base_url)
    if not base_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A Confluence base URL is required (e.g. https://your-org.atlassian.net).",
        )
    if cfg.auth_method not in ("cloud", "server"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "auth_method must be 'cloud' or 'server'."
        )
    if cfg.auth_method == "cloud" and not cfg.email.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confluence Cloud requires the Atlassian account email.",
        )
    existing_has_token = bool(existing and existing.cf_token)
    if not cfg.token and not existing_has_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "An API token (Cloud) or personal access token (Server) is required.",
        )

    src = existing or FolderSyncSource(folder_id=folder_id, source_type="confluence")
    if existing is not None and existing.source_type != "confluence":
        registry.clear_other_sources(src, "confluence")
    src.source_type = "confluence"
    src.cf_base_url = base_url
    src.cf_auth_method = cfg.auth_method
    src.cf_email = cfg.email.strip() or None
    if cfg.token:
        src.cf_token = cfg.token
    src.cf_selected_spaces = encode_spaces_field(
        [{"key": s.key, "name": s.name} for s in cfg.spaces]
    )
    src.cf_all_spaces = bool(cfg.all_spaces)
    src.cf_cql = cfg.cql.strip() or None
    src.cf_updated_since = validate_since(cfg.updated_since)
    return src


def trigger_check(src: FolderSyncSource) -> None:
    if not (src.cf_all_spaces or coerce_spaces_field(src.cf_selected_spaces)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one Confluence space (or enable 'all spaces').",
        )


registry.register(
    registry.SourceHandler(
        source_type="confluence",
        out_field="confluence",
        apply=apply_config,
        build_out=build_out,
        clear=clear_fields,
        trigger_check=trigger_check,
    )
)


class ConfluenceSpacesOut(BaseModel):
    spaces: list[ConfluenceSpace]


@router.get("/confluence/spaces", response_model=ConfluenceSpacesOut)
async def confluence_list_spaces_endpoint(
    folder_id: int,
    query: str = "",
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> ConfluenceSpacesOut:
    """Search Confluence spaces the stored credentials can see (for the picker).

    Reads the persisted row, so the user must Save base URL + token (and email
    for Cloud) before picking spaces. ``query`` filters locally (Confluence has
    no server-side space text search); an empty query returns the global list.
    """
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "confluence":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Confluence source")
    auth = AtlassianAuth(
        base_url=normalize_base_url(src.cf_base_url or ""),
        method=src.cf_auth_method or "cloud",
        email=src.cf_email or "",
        token=src.cf_token or "",
    )
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save the Confluence base URL and token (and email for Cloud) "
            "before listing spaces.",
        )
    try:
        spaces = await search_spaces(auth, query)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return ConfluenceSpacesOut(spaces=[ConfluenceSpace(**s) for s in spaces])
