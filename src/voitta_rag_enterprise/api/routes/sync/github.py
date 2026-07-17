"""GitHub sync source — schemas, PUT apply logic, and the branch picker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....db.models import FolderSyncSource
from ....services import events
from ....services.acl import CurrentUser
from ....services.sync.github import (
    GitAuth,
    coerce_branches_field,
    encode_branches_field,
    git_touch_scope,
    list_remote_branches,
)
from ...deps import current_user, db_session
from . import registry
from .base import check_owner, router

if TYPE_CHECKING:
    from .core import SyncSourceIn


class GithubSyncIn(BaseModel):
    """Payload for ``PUT /folders/{id}/sync`` when ``source_type == 'github'``."""

    repo: str = Field(..., min_length=1)
    path: str = ""
    branches: list[str] = Field(default_factory=list)
    all_branches: bool = False
    extended: bool = False
    # "agent" = use the server host's ssh-agent + ~/.ssh/config (the only mode
    # that works with hardware keys: YubiKey / SSHCA / touch2ssh).
    auth_method: Literal["ssh", "token", "agent", ""] = ""
    username: str = ""
    pat: str = ""
    ssh_key: str = ""


class GithubSyncOut(BaseModel):
    repo: str
    path: str
    branches: list[str]
    all_branches: bool
    extended: bool
    auth_method: str
    username: str
    has_pat: bool
    has_ssh_key: bool


def clear_fields(src: FolderSyncSource) -> None:
    src.gh_repo = None
    src.gh_path = None
    src.gh_branches = None
    src.gh_all_branches = False
    src.gh_extended = False
    src.gh_auth_method = None
    src.gh_username = None
    src.gh_pat = None
    src.gh_token = None


def build_out(src: FolderSyncSource) -> GithubSyncOut:
    return GithubSyncOut(
        repo=src.gh_repo or "",
        path=src.gh_path or "",
        branches=coerce_branches_field(src.gh_branches) or [],
        all_branches=bool(src.gh_all_branches),
        extended=bool(src.gh_extended),
        auth_method=src.gh_auth_method or "",
        username=src.gh_username or "",
        has_pat=bool(src.gh_pat),
        has_ssh_key=bool(src.gh_token),
    )


def apply_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
    **_: object,  # db/user context used only by credential-aware connectors
) -> FolderSyncSource:
    cfg = body.github
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Missing 'github' config for source_type='github'",
        )
    if not cfg.all_branches and not cfg.branches:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Either set all_branches=true or pick at least one branch",
        )
    if cfg.auth_method not in ("", "ssh", "token", "agent"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid auth_method: {cfg.auth_method!r}",
        )

    src = existing or FolderSyncSource(folder_id=folder_id, source_type="github")
    # Switching from a different source clears its credentials.
    if existing is not None and existing.source_type != "github":
        registry.clear_other_sources(src, "github")
    src.source_type = "github"
    src.gh_repo = cfg.repo.strip()
    src.gh_path = cfg.path.strip("/")
    src.gh_branches = encode_branches_field(cfg.branches)
    src.gh_all_branches = cfg.all_branches
    src.gh_extended = cfg.extended
    src.gh_auth_method = cfg.auth_method or None
    src.gh_username = cfg.username or None
    src.gh_pat = cfg.pat or None
    src.gh_token = cfg.ssh_key or None
    return src


registry.register(
    registry.SourceHandler(
        source_type="github",
        out_field="github",
        apply=apply_config,
        build_out=build_out,
        clear=clear_fields,
    )
)


class BranchListIn(BaseModel):
    repo: str
    auth_method: Literal["ssh", "token", "agent", ""] = ""
    username: str = ""
    pat: str = ""
    ssh_key: str = ""


class BranchListOut(BaseModel):
    branches: list[str]


@router.post("/branches", response_model=BranchListOut)
def list_branches(
    folder_id: int,
    body: BranchListIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> BranchListOut:
    """Helper for the UI's branch dropdown — runs ``git ls-remote --heads``.

    Auth is taken from the request body (the user is mid-form and the row
    might not have been saved yet).
    """
    check_owner(folder_id, db, user)
    auth = GitAuth(
        method=body.auth_method or "",
        ssh_key=body.ssh_key,
        username=body.username,
        pat=body.pat,
    )

    def _touch(state: str) -> None:
        # state: "wait" → show the YubiKey-touch banner; "done" → clear it.
        events.publish(
            "folders",
            {"type": "git.touch", "folder_id": folder_id, "state": state},
        )

    try:
        with git_touch_scope(_touch):
            branches = list_remote_branches(body.repo.strip(), auth)
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"git ls-remote failed: {e}"
        ) from e
    return BranchListOut(branches=branches)
