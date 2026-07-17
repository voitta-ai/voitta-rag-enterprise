"""Jira sync source — schemas, PUT apply logic, and the project picker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....db.models import FolderSyncSource
from ....services.acl import CurrentUser
from ....services.sync.atlassian_auth import AtlassianAuth, normalize_base_url
from ....services.sync.jira import (
    ISSUES_UPDATED_SINCE as JIRA_DEFAULT_SINCE,
)
from ....services.sync.jira import (
    coerce_projects_field,
    encode_projects_field,
    search_projects,
)
from ...deps import current_user, db_session
from . import registry
from .base import check_owner, router, validate_since

if TYPE_CHECKING:
    from .core import SyncSourceIn


class JiraProject(BaseModel):
    key: str
    name: str = ""


class JiraSyncIn(BaseModel):
    """Payload when ``source_type == 'jira'``."""

    base_url: str = ""
    auth_method: Literal["cloud", "server"] = "cloud"
    email: str = ""
    token: str = ""
    projects: list[JiraProject] = Field(default_factory=list)
    all_projects: bool = False
    jql: str = ""
    updated_since: str = ""  # YYYY-MM-DD recency floor; "" → connector default


class JiraSyncOut(BaseModel):
    base_url: str
    auth_method: str
    email: str
    projects: list[JiraProject]
    all_projects: bool
    jql: str
    updated_since: str   # stored recency floor, or "" when unset
    default_since: str   # the connector's built-in default (shown when unset)
    has_token: bool
    connected: bool  # true once base_url + token (+ email for cloud) are stored


def clear_fields(src: FolderSyncSource) -> None:
    src.jira_base_url = None
    src.jira_auth_method = None
    src.jira_email = None
    src.jira_token = None
    src.jira_selected_projects = None
    src.jira_all_projects = False
    src.jira_jql = None
    src.jira_updated_since = None


def build_out(src: FolderSyncSource) -> JiraSyncOut:
    method = src.jira_auth_method or "cloud"
    base = src.jira_base_url or ""
    token_set = bool(src.jira_token)
    email_set = bool(src.jira_email)
    return JiraSyncOut(
        base_url=base,
        auth_method=method,
        email=src.jira_email or "",
        projects=[
            JiraProject(**p)
            for p in coerce_projects_field(src.jira_selected_projects)
        ],
        all_projects=bool(src.jira_all_projects),
        jql=src.jira_jql or "",
        updated_since=src.jira_updated_since or "",
        default_since=JIRA_DEFAULT_SINCE,
        has_token=token_set,
        connected=bool(base and token_set and (method == "server" or email_set)),
    )


def apply_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
    **_: object,  # db/user context used only by credential-aware connectors
) -> FolderSyncSource:
    """Apply a ``jira`` payload to a (new or existing) row.

    Cloud needs an email (Basic ``email:token``); server needs only the PAT.
    The token input is masked client-side, so a blank ``token`` on an existing
    row means "leave the stored one alone", not "clear it".
    """
    cfg = body.jira
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Missing 'jira' config"
        )
    base_url = normalize_base_url(cfg.base_url)
    if not base_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A Jira base URL is required (e.g. https://your-org.atlassian.net).",
        )
    if cfg.auth_method not in ("cloud", "server"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "auth_method must be 'cloud' or 'server'.",
        )
    if cfg.auth_method == "cloud" and not cfg.email.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Jira Cloud requires the Atlassian account email.",
        )
    existing_has_token = bool(existing and existing.jira_token)
    if not cfg.token and not existing_has_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "An API token (Cloud) or personal access token (Server) is required.",
        )

    src = existing or FolderSyncSource(folder_id=folder_id, source_type="jira")
    if existing is not None and existing.source_type != "jira":
        registry.clear_other_sources(src, "jira")
    src.source_type = "jira"
    src.jira_base_url = base_url
    src.jira_auth_method = cfg.auth_method
    src.jira_email = cfg.email.strip() or None
    if cfg.token:
        src.jira_token = cfg.token
    src.jira_selected_projects = encode_projects_field(
        [{"key": p.key, "name": p.name} for p in cfg.projects]
    )
    src.jira_all_projects = bool(cfg.all_projects)
    src.jira_jql = cfg.jql.strip() or None
    src.jira_updated_since = validate_since(cfg.updated_since)
    return src


def trigger_check(src: FolderSyncSource) -> None:
    if not (src.jira_all_projects or coerce_projects_field(src.jira_selected_projects)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one Jira project (or enable 'all projects').",
        )


registry.register(
    registry.SourceHandler(
        source_type="jira",
        out_field="jira",
        apply=apply_config,
        build_out=build_out,
        clear=clear_fields,
        trigger_check=trigger_check,
    )
)


class JiraProjectsOut(BaseModel):
    projects: list[JiraProject]


@router.get("/jira/projects", response_model=JiraProjectsOut)
async def jira_list_projects_endpoint(
    folder_id: int,
    query: str = "",
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> JiraProjectsOut:
    """Search Jira projects the stored credentials can see (for the picker).

    Reads the persisted row, so the user must Save base URL + token (and email
    for Cloud) before picking projects — same flow as the SharePoint picker.
    ``query`` filters server-side (Cloud) so large tenants aren't downloaded in
    full; an empty query returns the first page.
    """
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "jira":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Jira source")
    auth = AtlassianAuth(
        base_url=normalize_base_url(src.jira_base_url or ""),
        method=src.jira_auth_method or "cloud",
        email=src.jira_email or "",
        token=src.jira_token or "",
    )
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save the Jira base URL and token (and email for Cloud) before "
            "listing projects.",
        )
    try:
        projects = await search_projects(auth, query)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return JiraProjectsOut(projects=[JiraProject(**p) for p in projects])
