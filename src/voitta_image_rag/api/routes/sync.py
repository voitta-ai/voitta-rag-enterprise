"""Folder sync configuration + trigger endpoints.

Per-folder routes live under ``/folders/{folder_id}/sync``; the OAuth
callback (which has to be at a fixed URL Google can return to) lives under
``/sync/oauth/google/callback``.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db.models import File, Folder, FolderSyncSource
from ...services import events, job_queue
from ...services.acl import CurrentUser, user_can_see_folder
from ...services.sync.github import (
    GitAuth,
    coerce_branches_field,
    encode_branches_field,
    list_remote_branches,
)
from ...services.sync.google_drive import (
    coerce_folders_field as gd_coerce_folders,
)
from ...services.sync.google_drive import (
    encode_folders_field as gd_encode_folders,
)
from ...services.sync.google_drive import (
    exchange_code_for_tokens as gd_exchange_code,
)
from ...services.sync.google_drive import (
    get_auth_url as gd_get_auth_url,
)
from ...services.sync.google_drive import (
    list_root_folders as gd_list_root_folders,
)
from ..deps import current_user, db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/folders/{folder_id}/sync", tags=["sync"])

# Separate router with no folder_id in the path — Google's OAuth callback
# URL is registered once with the Google client and cannot be parameterised.
oauth_router = APIRouter(prefix="/sync", tags=["sync"])


class GithubSyncIn(BaseModel):
    """Payload for ``PUT /folders/{id}/sync`` when ``source_type == 'github'``."""

    repo: str = Field(..., min_length=1)
    path: str = ""
    branches: list[str] = Field(default_factory=list)
    all_branches: bool = False
    extended: bool = False
    auth_method: Literal["ssh", "token", ""] = ""
    username: str = ""
    pat: str = ""
    ssh_key: str = ""


class GoogleDriveFolder(BaseModel):
    """One Drive folder selection: identity + display name shown in the UI."""

    id: str
    name: str = ""


class GoogleDriveSyncIn(BaseModel):
    """Payload for ``PUT /folders/{id}/sync`` when ``source_type == 'google_drive'``."""

    client_id: str = ""
    client_secret: str = ""
    folders: list[GoogleDriveFolder] = Field(default_factory=list)
    service_account_json: str = ""


class SyncSourceIn(BaseModel):
    source_type: Literal["github", "google_drive"]
    github: GithubSyncIn | None = None
    google_drive: GoogleDriveSyncIn | None = None


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


class GoogleDriveSyncOut(BaseModel):
    client_id: str
    folders: list[GoogleDriveFolder]
    has_client_secret: bool
    has_service_account: bool
    connected: bool  # true once a refresh_token has been stored


class SyncSourceOut(BaseModel):
    folder_id: int
    source_type: str
    sync_status: str
    sync_error: str | None
    last_synced_at: int | None
    github: GithubSyncOut | None = None
    google_drive: GoogleDriveSyncOut | None = None


def _to_out(src: FolderSyncSource) -> SyncSourceOut:
    gh = None
    gd = None
    if src.source_type == "github":
        gh = GithubSyncOut(
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
    elif src.source_type == "google_drive":
        gd = GoogleDriveSyncOut(
            client_id=src.gd_client_id or "",
            folders=[
                GoogleDriveFolder(**f) for f in gd_coerce_folders(src.gd_folder_id)
            ],
            has_client_secret=bool(src.gd_client_secret),
            has_service_account=bool(src.gd_service_account_json),
            connected=bool(src.gd_refresh_token),
        )
    return SyncSourceOut(
        folder_id=src.folder_id,
        source_type=src.source_type,
        sync_status=src.sync_status,
        sync_error=src.sync_error,
        last_synced_at=src.last_synced_at,
        github=gh,
        google_drive=gd,
    )


def _check_access(
    folder_id: int, db: Session, user: CurrentUser
) -> Folder:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return folder


def _folder_has_real_files(db: Session, folder_id: int, folder_root: Path) -> bool:
    """Empty = no indexed files AND no on-disk files (other than sync sidecars)."""
    from sqlalchemy import select

    has_db_files = (
        db.execute(
            select(File.id).where(
                File.folder_id == folder_id, File.state != "deleted"
            ).limit(1)
        ).first()
        is not None
    )
    if has_db_files:
        return True
    if not folder_root.exists():
        return False
    for entry in folder_root.iterdir():
        if entry.name in (".voitta_sources.json", ".voitta_timestamps.json"):
            continue
        return True
    return False


@router.get("", response_model=SyncSourceOut | None)
def get_sync_source(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut | None:
    _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    return _to_out(src) if src is not None else None


@router.put("", response_model=SyncSourceOut)
def upsert_sync_source(
    folder_id: int,
    body: SyncSourceIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut:
    """Create or update a sync source.

    First-time configuration requires the folder to be empty (no indexed files,
    no on-disk files other than sync sidecars). Reconfiguration of an existing
    source is always allowed — adding/removing branches, switching extended on
    or off, rotating credentials, etc. The next sync will reconcile contents.
    """
    folder = _check_access(folder_id, db, user)
    existing = db.get(FolderSyncSource, folder_id)

    if existing is None and _folder_has_real_files(db, folder_id, Path(folder.path)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Sync can only be configured on empty folders",
        )

    if body.source_type == "github":
        if body.github is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Missing 'github' config for source_type='github'",
            )
        gh_cfg = body.github
        if not gh_cfg.all_branches and not gh_cfg.branches:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Either set all_branches=true or pick at least one branch",
            )
        if gh_cfg.auth_method not in ("", "ssh", "token"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid auth_method: {gh_cfg.auth_method!r}",
            )

        src = existing or FolderSyncSource(folder_id=folder_id, source_type="github")
        # Switching from a different source clears its credentials.
        if existing is not None and existing.source_type != "github":
            _clear_google_drive_fields(src)
        src.source_type = "github"
        src.gh_repo = gh_cfg.repo.strip()
        src.gh_path = gh_cfg.path.strip("/")
        src.gh_branches = encode_branches_field(gh_cfg.branches)
        src.gh_all_branches = gh_cfg.all_branches
        src.gh_extended = gh_cfg.extended
        src.gh_auth_method = gh_cfg.auth_method or None
        src.gh_username = gh_cfg.username or None
        src.gh_pat = gh_cfg.pat or None
        src.gh_token = gh_cfg.ssh_key or None

    elif body.source_type == "google_drive":
        if body.google_drive is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Missing 'google_drive' config for source_type='google_drive'",
            )
        gd_cfg = body.google_drive
        # ``has_client_secret`` is true when only the public client_id was
        # re-sent for an existing row (the secret stays masked client-side
        # and is only re-posted when the user types a new one).
        existing_has_secret = bool(existing and existing.gd_client_secret)
        has_oauth = bool(
            gd_cfg.client_id and (gd_cfg.client_secret or existing_has_secret)
        )
        has_sa = bool(gd_cfg.service_account_json)
        if not has_oauth and not has_sa:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Provide either OAuth client_id+client_secret or a service-account JSON",
            )
        # folder_id intentionally NOT validated here. Picking it requires a
        # connected OAuth account, which requires a saved client_id/secret —
        # so the user has to Save with no folder_id first, Connect, then
        # come back to fill the folder ID. The trigger endpoint enforces it.

        src = existing or FolderSyncSource(
            folder_id=folder_id, source_type="google_drive"
        )
        if existing is not None and existing.source_type != "google_drive":
            _clear_github_fields(src)
            # New source type → drop any stored refresh_token, it belongs to
            # whatever client we were using before.
            src.gd_refresh_token = None
        src.source_type = "google_drive"
        src.gd_client_id = gd_cfg.client_id.strip() or None
        # Preserve the stored secret when the form re-posts an empty value —
        # the input is masked, so a blank submission means "leave alone",
        # not "clear it". Same for service_account_json.
        if gd_cfg.client_secret:
            src.gd_client_secret = gd_cfg.client_secret
        if gd_cfg.service_account_json:
            src.gd_service_account_json = gd_cfg.service_account_json
        src.gd_folder_id = gd_encode_folders(
            [{"id": f.id, "name": f.name} for f in gd_cfg.folders]
        )
        # Refresh-token field is set by the OAuth callback, never by save.

    else:  # pragma: no cover — Pydantic Literal guards this
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported source_type: {body.source_type!r}",
        )

    if existing is None:
        db.add(src)
    db.commit()
    db.refresh(src)
    if existing is None:
        # Toolbar visibility & label depend on has_sync_source — push the
        # update so the UI doesn't need to refetch the folder list.
        _publish_folder_changed(folder, has_sync_source=True)
    return _to_out(src)


def _clear_github_fields(src: FolderSyncSource) -> None:
    src.gh_repo = None
    src.gh_path = None
    src.gh_branches = None
    src.gh_all_branches = False
    src.gh_extended = False
    src.gh_auth_method = None
    src.gh_username = None
    src.gh_pat = None
    src.gh_token = None


def _clear_google_drive_fields(src: FolderSyncSource) -> None:
    src.gd_client_id = None
    src.gd_client_secret = None
    src.gd_refresh_token = None
    src.gd_service_account_json = None
    src.gd_folder_id = None


def _publish_folder_changed(folder: Folder, *, has_sync_source: bool) -> None:
    events.publish(
        "folders",
        {
            "type": "folder.upserted",
            "folder": {
                "id": folder.id,
                "path": folder.path,
                "display_name": folder.display_name,
                "source_type": folder.source_type,
                "enabled": folder.enabled,
                "managed": folder.managed,
                "created_at": folder.created_at,
                "has_sync_source": has_sync_source,
            },
        },
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_sync_source(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        return
    db.delete(src)
    db.commit()
    _publish_folder_changed(folder, has_sync_source=False)


class SyncTriggerOut(BaseModel):
    folder_id: int
    job_id: int


@router.post("/trigger", response_model=SyncTriggerOut)
def trigger_sync(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncTriggerOut:
    """Enqueue a sync job. Dedup'd per folder so spam-clicking is harmless."""
    _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No sync source configured"
        )
    if src.source_type == "google_drive" and not gd_coerce_folders(src.gd_folder_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one Google Drive folder before syncing",
        )
    job_id = job_queue.enqueue(
        db,
        "sync",
        {"folder_id": folder_id},
        dedup_key=f"sync:{folder_id}",
    )
    db.commit()
    return SyncTriggerOut(folder_id=folder_id, job_id=job_id)


class BranchListIn(BaseModel):
    repo: str
    auth_method: Literal["ssh", "token", ""] = ""
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
    _check_access(folder_id, db, user)
    auth = GitAuth(
        method=body.auth_method or "",
        ssh_key=body.ssh_key,
        username=body.username,
        pat=body.pat,
    )
    try:
        branches = list_remote_branches(body.repo.strip(), auth)
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"git ls-remote failed: {e}"
        ) from e
    return BranchListOut(branches=branches)


# ---------------------------------------------------------------------------
# Google Drive helpers — OAuth init / callback + folder picker.
#
# The OAuth callback URL is registered once in the Google client and cannot
# carry the folder_id. We pass the folder_id through OAuth state instead.
# ---------------------------------------------------------------------------


class GdAuthInitOut(BaseModel):
    auth_url: str


def _oauth_redirect_uri(request: Request) -> str:
    """Build the redirect URI the way Google's consent screen will see it.

    ``request.base_url`` reflects what the ASGI server received on the wire,
    which is plain HTTP when a reverse proxy (Cloudflare, Caddy, nginx)
    terminates TLS in front of the app. Google then rejects the code-
    exchange with ``redirect_uri_mismatch`` because the value we registered
    is ``https://…``. Read ``X-Forwarded-Proto`` and ``X-Forwarded-Host``
    ourselves so this works without requiring uvicorn to be launched with
    ``--proxy-headers``.

    Falls back to ``request.url`` for the localhost / no-proxy case.
    """
    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    proto = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{proto}://{host}/api/sync/oauth/google/callback"


@router.post("/google-drive/auth", response_model=GdAuthInitOut)
def gd_auth_init(
    folder_id: int,
    request: Request,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> GdAuthInitOut:
    """Build the Google OAuth URL the UI should pop open in a new window.

    ``state`` carries the folder id so the callback (which is folder-agnostic
    in its URL) can find the right row.
    """
    _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Configure Google Drive (client_id, client_secret) before connecting",
        )
    if not (src.gd_client_id and src.gd_client_secret):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save client_id and client_secret before connecting",
        )
    state = base64.urlsafe_b64encode(str(folder_id).encode()).decode()
    auth_url = gd_get_auth_url(
        client_id=src.gd_client_id,
        redirect_uri=_oauth_redirect_uri(request),
        state=state,
    )
    return GdAuthInitOut(auth_url=auth_url)


class GdFoldersOut(BaseModel):
    folders: list[dict[str, str]]
    shared_folders: list[dict[str, str]]
    shared_drives: list[dict[str, str]]


@router.get("/google-drive/folders", response_model=GdFoldersOut)
async def gd_list_folders(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> GdFoldersOut:
    """List Drive locations the user can pick as a sync root."""
    _check_access(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Drive source")
    if not src.gd_refresh_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google Drive not connected — click Connect first",
        )
    try:
        data = await gd_list_root_folders(
            src.gd_client_id or "",
            src.gd_client_secret or "",
            src.gd_refresh_token or "",
        )
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return GdFoldersOut(**data)


@oauth_router.get("/oauth/google/callback")
async def gd_oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> HTMLResponse:
    """Finishes the Google OAuth dance: exchange code → store refresh_token."""
    try:
        folder_id = int(base64.urlsafe_b64decode(state.encode()).decode())
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid state parameter"
        ) from e

    from ...db.database import session_scope

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is None or src.source_type != "google_drive":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Google Drive source not found for this state",
            )
        client_id = src.gd_client_id or ""
        client_secret = src.gd_client_secret or ""

    try:
        tokens = await gd_exchange_code(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=_oauth_redirect_uri(request),
        )
    except Exception as e:
        logger.exception("Google OAuth callback failed for folder %s", folder_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        # ``prompt=consent`` is supposed to guarantee a refresh_token, but
        # some Workspace policies still strip it. Surface a clear message.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google did not return a refresh_token. Revoke the app's access "
            "in your Google Account and try again.",
        )

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is not None:
            src.gd_refresh_token = refresh_token

    events.publish(
        "folders",
        {
            "type": "folder.gd_connected",
            "folder_id": folder_id,
        },
    )

    # The popup self-closes; the opening tab listens on the events stream.
    return HTMLResponse(
        "<html><body><script>window.close()</script>"
        "<p>Google Drive connected. You can close this tab.</p></body></html>"
    )
