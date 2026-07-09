"""Google Drive sync source (API-based) — schemas, PUT apply logic, the
OAuth init/callback pair, and the Drive folder pickers.

The OAuth callback URL is registered once in the Google client and cannot
carry the folder_id. We pass the folder_id through OAuth state instead.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....config import get_settings
from ....db.models import FolderSyncSource
from ....services import events
from ....services.acl import CurrentUser
from ....services.sync.google_drive import (
    GoogleDriveAuth,
    GoogleDriveConnector,
    GoogleWorkspaceAccessError,
    coerce_folders_field,
    effective_oauth_client,
    encode_folders_field,
)
from ....services.sync.google_drive import (
    exchange_code_for_tokens as gd_exchange_code,
)
from ....services.sync.google_drive import (
    get_auth_url as gd_get_auth_url,
)
from ....services.sync.google_drive import (
    list_folder_children as gd_list_folder_children,
)
from ....services.sync.google_drive import (
    list_root_folders as gd_list_root_folders,
)
from ...deps import current_user, db_session
from . import registry
from .base import check_owner, external_redirect_uri, oauth_router, router

if TYPE_CHECKING:
    from .core import SyncSourceIn

logger = logging.getLogger(__name__)


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
    # When True the OAuth flow uses the localhost-loopback redirect URI
    # (the admin then registers only the localhost URL in GCP and runs
    # a small local nginx bridge). Default False = original behaviour.
    use_loopback: bool = False
    # When True, sync downloads only ordinary binary files and skips
    # Google-native Docs/Sheets/Slides/Forms — so it works even when the
    # project hasn't enabled those Workspace APIs (only Drive is required).
    files_only: bool = False
    # When True the OAuth creds come from the deploy's built-in
    # Desktop-app client (desktop/single-user only) — client_id and
    # client_secret above are then ignored.
    use_builtin: bool = False


class GoogleDriveSyncOut(BaseModel):
    client_id: str
    folders: list[GoogleDriveFolder]
    has_client_secret: bool
    has_service_account: bool
    connected: bool  # true once a refresh_token has been stored
    use_loopback: bool = False
    files_only: bool = False
    use_builtin: bool = False


class GoogleDriveApiStatusOut(BaseModel):
    """Result of probing which Workspace APIs the OAuth client can use."""

    drive: bool
    docs: bool
    sheets: bool
    slides: bool
    forms: bool
    # Convenience rollups for the UI: Drive is fatal if down; native_ok
    # means all four export APIs (Docs/Sheets/Slides/Forms) are enabled.
    drive_ok: bool
    native_ok: bool
    # True when the OAuth token lacks required scopes (reconnect, don't
    # "enable API"). When set, the per-API flags below it are unreliable.
    scope_problem: bool = False
    # (api_label, gcp_activation_url) for each disabled API.
    disabled: list[tuple[str, str]] = Field(default_factory=list)


def clear_fields(src: FolderSyncSource) -> None:
    src.gd_client_id = None
    src.gd_client_secret = None
    src.gd_refresh_token = None
    src.gd_service_account_json = None
    src.gd_folder_id = None
    src.gd_use_loopback = False
    src.gd_use_builtin = False
    src.gd_files_only = False


def build_out(src: FolderSyncSource) -> GoogleDriveSyncOut:
    return GoogleDriveSyncOut(
        # Built-in rows echo a blank client_id — the SPA doesn't need
        # the shipped client's identity and mustn't render it.
        client_id="" if src.gd_use_builtin else (src.gd_client_id or ""),
        folders=[
            GoogleDriveFolder(**f) for f in coerce_folders_field(src.gd_folder_id)
        ],
        has_client_secret=bool(src.gd_client_secret),
        has_service_account=bool(src.gd_service_account_json),
        connected=bool(src.gd_refresh_token),
        use_loopback=bool(src.gd_use_loopback),
        files_only=bool(src.gd_files_only),
        use_builtin=bool(src.gd_use_builtin),
    )


def builtin_available() -> bool:
    """True when the built-in Drive OAuth client may be used here.

    Requires single-user (desktop) mode — the consent redirect targets
    this server's own host, reachable at 127.0.0.1 only when browser and
    server share a machine — plus the baked/env client credentials.
    """
    s = get_settings()
    return bool(
        s.single_user and s.gd_builtin_client_id and s.gd_builtin_client_secret
    )


def _effective_client(src: FolderSyncSource) -> tuple[str, str]:
    """Row's effective OAuth (client_id, client_secret), as an HTTP 400
    when the built-in client is referenced but unavailable (env removed,
    or a desktop DB imported into a hosted deploy)."""
    try:
        return effective_oauth_client(src)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def apply_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
) -> FolderSyncSource:
    cfg = body.google_drive
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Missing 'google_drive' config for source_type='google_drive'",
        )
    # ``has_client_secret`` is true when only the public client_id was
    # re-sent for an existing row (the secret stays masked client-side
    # and is only re-posted when the user types a new one). Same idea
    # applies to the service-account JSON: a re-save without retyping
    # the SA blob shouldn't lose the existing one.
    existing_has_secret = bool(existing and existing.gd_client_secret)
    existing_has_sa = bool(existing and existing.gd_service_account_json)
    has_oauth = bool(cfg.client_id and (cfg.client_secret or existing_has_secret))
    has_sa = bool(cfg.service_account_json or existing_has_sa)
    if cfg.use_builtin and not builtin_available():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Built-in Google sign-in is not available on this deployment",
        )
    if not cfg.use_builtin and not has_oauth and not has_sa:
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
        registry.clear_other_sources(src, "google_drive")
        # New source type → drop any stored refresh_token, it belongs to
        # whatever client we were using before.
        src.gd_refresh_token = None
    src.source_type = "google_drive"
    src.gd_client_id = cfg.client_id.strip() or None
    # Preserve the stored secret when the form re-posts an empty value —
    # the input is masked, so a blank submission means "leave alone",
    # not "clear it". Same for service_account_json.
    if cfg.client_secret:
        src.gd_client_secret = cfg.client_secret
    if cfg.service_account_json:
        src.gd_service_account_json = cfg.service_account_json
    src.gd_folder_id = encode_folders_field(
        [{"id": f.id, "name": f.name} for f in cfg.folders]
    )
    # Refresh-token field is set by the OAuth callback, never by save.
    # Loopback flag: switching it invalidates any refresh_token that
    # was issued under the other redirect URI (Google scopes refresh
    # tokens to the redirect URI used at consent), so drop it.
    # Same for the builtin flag: the token was minted by one client
    # (built-in or user-supplied) and won't refresh under the other.
    prev_loopback = bool(existing and existing.gd_use_loopback)
    prev_builtin = bool(existing and existing.gd_use_builtin)
    if cfg.use_builtin:
        # Builtin flow always uses the request-host redirect URI —
        # the loopback bridge is a hosted-deploy workaround.
        cfg.use_loopback = False
    if (
        prev_loopback != bool(cfg.use_loopback)
        or prev_builtin != bool(cfg.use_builtin)
    ):
        src.gd_refresh_token = None
    src.gd_use_loopback = bool(cfg.use_loopback)
    src.gd_use_builtin = bool(cfg.use_builtin)
    src.gd_files_only = bool(cfg.files_only)
    return src


def trigger_check(src: FolderSyncSource) -> None:
    if not coerce_folders_field(src.gd_folder_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one Google Drive folder before syncing",
        )


registry.register(
    registry.SourceHandler(
        source_type="google_drive",
        out_field="google_drive",
        apply=apply_config,
        build_out=build_out,
        clear=clear_fields,
        trigger_check=trigger_check,
    )
)


# ---------------------------------------------------------------------------
# OAuth init / callback + folder pickers
# ---------------------------------------------------------------------------


class GdAuthInitOut(BaseModel):
    auth_url: str


# Port used by the optional localhost-loopback redirect URI. Hardcoded
# on purpose — admins register this exact URL in GCP, and a small
# nginx bridge on their machine listens on this port and proxies the
# callback back to this server. Keep in sync with the bridge config.
GD_LOOPBACK_PORT = 53682
GD_LOOPBACK_REDIRECT_URI = (
    f"http://localhost:{GD_LOOPBACK_PORT}/api/sync/oauth/google/callback"
)


def _oauth_redirect_uri(request: Request, *, use_loopback: bool = False) -> str:
    return external_redirect_uri(
        request,
        "/api/sync/oauth/google/callback",
        loopback_uri=GD_LOOPBACK_REDIRECT_URI if use_loopback else None,
    )


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
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Configure Google Drive (client_id, client_secret) before connecting",
        )
    if not src.gd_use_builtin and not (src.gd_client_id and src.gd_client_secret):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save client_id and client_secret before connecting",
        )
    client_id, _ = _effective_client(src)
    state = base64.urlsafe_b64encode(str(folder_id).encode()).decode()
    auth_url = gd_get_auth_url(
        client_id=client_id,
        redirect_uri=_oauth_redirect_uri(request, use_loopback=bool(src.gd_use_loopback)),
        state=state,
    )
    return GdAuthInitOut(auth_url=auth_url)


class GdDrivePickEntry(BaseModel):
    """One row in the Drive folder picker.

    ``owner_*`` and the timestamps are populated for ``folders`` /
    ``shared_folders`` (per-user folders have one owner) and left empty
    for ``shared_drives`` (no per-drive owner concept).
    """

    id: str
    name: str
    owner_email: str = ""
    owner_name: str = ""
    shared_at: str = ""  # ISO8601 from Drive's ``sharedWithMeTime``
    modified_at: str = ""  # ISO8601 from Drive's ``modifiedTime``


class GdFoldersOut(BaseModel):
    folders: list[GdDrivePickEntry]
    shared_folders: list[GdDrivePickEntry]
    shared_drives: list[GdDrivePickEntry]


@router.get("/google-drive/folders", response_model=GdFoldersOut)
async def gd_list_folders(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> GdFoldersOut:
    """List Drive locations the user can pick as a sync root.

    Works for both auth modes. With OAuth we use the stored refresh_token.
    With a service account we mint a short-lived access token from the
    saved JSON key — the SA only sees folders explicitly shared with its
    ``client_email``, plus any Shared Drives it's a member of, but those
    populate the same picker UI.
    """
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Drive source")
    has_oauth = bool(src.gd_refresh_token)
    has_sa = bool(src.gd_service_account_json)
    if not has_oauth and not has_sa:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect via OAuth or save a service-account JSON before listing folders",
        )
    client_id, client_secret = _effective_client(src)
    try:
        data = await gd_list_root_folders(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=src.gd_refresh_token or "",
            service_account_json=src.gd_service_account_json or "",
        )
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return GdFoldersOut(**data)


@router.get("/google-drive/browse")
async def gd_browse_folder(
    folder_id: int,
    parent_id: str = Query(..., description="Drive folder ID whose children to list"),
    drive_id: str = Query("", description="Shared Drive ID (leave empty for My Drive / Shared-with-me)"),
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[dict]:
    """Return immediate subfolder children of a Drive folder for the tree picker."""
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Drive source")
    if not src.gd_refresh_token and not src.gd_service_account_json:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not connected to Google Drive")
    client_id, client_secret = _effective_client(src)
    try:
        children = await gd_list_folder_children(
            parent_id=parent_id,
            drive_id=drive_id,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=src.gd_refresh_token or "",
            service_account_json=src.gd_service_account_json or "",
        )
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return children


@router.get("/google-drive/api-status", response_model=GoogleDriveApiStatusOut)
async def gd_api_status(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> GoogleDriveApiStatusOut:
    """Probe which Workspace APIs are enabled for this folder's OAuth client.

    Powers the sync modal's "Test API availability" button. Drive being
    down is fatal (nothing can sync); Docs/Sheets/Slides/Forms being down
    is recoverable by enabling files-only sync. Never enforces a policy —
    it just reports — so the UI can guide the user.
    """
    check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Drive source")
    if not src.gd_refresh_token and not src.gd_service_account_json:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect via OAuth or save a service-account JSON before testing APIs",
        )
    client_id, client_secret = _effective_client(src)
    auth = GoogleDriveAuth(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=src.gd_refresh_token or "",
        service_account_json=src.gd_service_account_json or "",
    )
    try:
        st = await asyncio.to_thread(GoogleDriveConnector().probe_apis, auth)
    except GoogleWorkspaceAccessError as e:
        # probe_apis itself doesn't raise this, but token refresh / build
        # can surface auth problems — relay as a 502 like other GD calls.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    return GoogleDriveApiStatusOut(
        drive=st.drive,
        docs=st.docs,
        sheets=st.sheets,
        slides=st.slides,
        forms=st.forms,
        drive_ok=st.drive,
        native_ok=st.native_ok,
        scope_problem=st.scope_problem,
        disabled=st.disabled,
    )


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

    from ....db.database import session_scope

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is None or src.source_type != "google_drive":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Google Drive source not found for this state",
            )
        client_id, client_secret = _effective_client(src)
        use_loopback = bool(src.gd_use_loopback)

    try:
        tokens = await gd_exchange_code(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=_oauth_redirect_uri(request, use_loopback=use_loopback),
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
