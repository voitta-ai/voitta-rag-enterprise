"""Folder sync configuration + trigger endpoints.

Per-folder routes live under ``/folders/{folder_id}/sync``; the OAuth
callback (which has to be at a fixed URL Google can return to) lives under
``/sync/oauth/google/callback``.
"""

from __future__ import annotations

import asyncio
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
from ...services.acl import CurrentUser, is_folder_owner, user_can_see_folder
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
    list_folder_children as gd_list_folder_children,
    list_root_folders as gd_list_root_folders,
)
from ...services.sync.google_drive import (
    GoogleDriveAuth,
    GoogleDriveConnector,
    GoogleWorkspaceAccessError,
)
from ...services.sync import microsoft_auth as msa
from ...services.sync.sharepoint import (
    coerce_sites_field as sp_coerce_sites,
    encode_sites_field as sp_encode_sites,
    list_all_sites as sp_list_all_sites,
)
from ...services.sync.teams import list_tenant_users as tm_list_tenant_users
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
    # When True the OAuth flow uses the localhost-loopback redirect URI
    # (the admin then registers only the localhost URL in GCP and runs
    # a small local nginx bridge). Default False = original behaviour.
    use_loopback: bool = False
    # When True, sync downloads only ordinary binary files and skips
    # Google-native Docs/Sheets/Slides/Forms — so it works even when the
    # project hasn't enabled those Workspace APIs (only Drive is required).
    files_only: bool = False


class SharePointSite(BaseModel):
    """One site row in the SharePoint picker."""

    id: str
    displayName: str = ""
    webUrl: str = ""


class SharePointSyncIn(BaseModel):
    """Payload when ``source_type == 'sharepoint'``."""

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_pem: str = ""
    auth_method: Literal["", "oauth", "app_secret", "app_cert"] = ""
    sites: list[SharePointSite] = Field(default_factory=list)
    all_sites: bool = False
    use_loopback: bool = False


class TeamsSyncIn(BaseModel):
    """Payload when ``source_type == 'teams'``."""

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_pem: str = ""
    auth_method: Literal["", "oauth", "app_secret", "app_cert"] = ""
    user_mode: Literal["me", "specific", "all_users"] = "me"
    user_id: str = ""
    include_attended: bool = True
    use_loopback: bool = False


class NfsSyncIn(BaseModel):
    """Payload for ``PUT /folders/{id}/sync`` when ``source_type == 'nfs'``.

    The admin-defined NFS root lives in ``admin_store.settings`` and is
    *not* passed in the payload — the sync source records only the
    user-chosen relative subpaths below the root.

    ``subpath`` is kept for backwards-compatibility: old clients post a
    single string; new clients post ``subpaths: list[str]``. The
    upsert handler folds the legacy field into the array if present.
    """

    subpath: str = ""
    subpaths: list[str] = Field(default_factory=list)


class SyncSourceIn(BaseModel):
    source_type: Literal["github", "google_drive", "nfs", "sharepoint", "teams"]
    github: GithubSyncIn | None = None
    google_drive: GoogleDriveSyncIn | None = None
    nfs: NfsSyncIn | None = None
    sharepoint: SharePointSyncIn | None = None
    teams: TeamsSyncIn | None = None
    # Periodic auto-sync. ``auto_sync_hours`` is bounded to 1-24 by the
    # scheduler — wider intervals belong to a real cron, not this in-process
    # loop. Both fields are common to all source types so they live on the
    # outer envelope, not inside the per-source blocks.
    auto_sync_enabled: bool = False
    auto_sync_hours: int = Field(default=6, ge=1, le=24)


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
    use_loopback: bool = False
    files_only: bool = False


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


class SharePointSyncOut(BaseModel):
    tenant_id: str
    client_id: str
    auth_method: str
    sites: list[SharePointSite]
    all_sites: bool
    has_client_secret: bool
    has_cert: bool
    use_loopback: bool
    connected: bool  # true once a refresh_token has been stored (oauth only)


class TeamsSyncOut(BaseModel):
    tenant_id: str
    client_id: str
    auth_method: str
    user_mode: str
    user_id: str
    include_attended: bool
    has_client_secret: bool
    has_cert: bool
    use_loopback: bool
    connected: bool


class NfsSyncOut(BaseModel):
    # Multi-subpath selection. Always sent as ``subpaths``; the legacy
    # single-string ``subpath`` field is echoed for old clients that
    # still read it (= the first element of ``subpaths`` for compat).
    subpath: str
    subpaths: list[str]
    # Snapshot of the current admin-side NFS root + its availability,
    # so the modal can show the resolved absolute path without a
    # second roundtrip and gate the Save button on availability.
    nfs_root: str
    nfs_available: bool
    nfs_status: str


class GoogleDriveLocalSyncOut(BaseModel):
    """Status for ``source_type == 'google_drive_local'`` (desktop, no creds)."""

    account: str       # signed-in Google account email
    path: str          # chosen subtree under ~/Library/CloudStorage
    available: bool    # macOS + Drive app running + mount live right now
    status: str        # "ok" | "offline" | "missing" | "unsupported"


class SyncSourceOut(BaseModel):
    folder_id: int
    source_type: str
    sync_status: str
    sync_error: str | None
    last_synced_at: int | None
    auto_sync_enabled: bool
    auto_sync_hours: int
    github: GithubSyncOut | None = None
    google_drive: GoogleDriveSyncOut | None = None
    google_drive_local: GoogleDriveLocalSyncOut | None = None
    nfs: NfsSyncOut | None = None
    sharepoint: SharePointSyncOut | None = None
    teams: TeamsSyncOut | None = None


def _nfs_status_snapshot() -> tuple[str, bool, str]:
    """Return ``(nfs_root, available, status)`` for inclusion in NFS
    sync-source responses. Re-checks on every call — an unmounted NFS
    flips the feature off without a restart."""
    from ...services.admin_store import get_nfs_root

    root = get_nfs_root()
    if not root:
        return "", False, "disabled"
    from pathlib import Path

    p = Path(root)
    if not p.exists():
        return root, False, "missing"
    if not p.is_dir():
        return root, False, "not_a_directory"
    try:
        next(iter(p.iterdir()), None)
    except (PermissionError, OSError):
        return root, False, "unreadable"
    return root, True, "ok"


def _gdl_status_snapshot(path: str) -> tuple[bool, str]:
    """Return ``(available, status)`` for a google_drive_local source.

    Re-checked on every render so an offline Drive app / signed-out account
    shows as unavailable without a restart.
    """
    from ...services.sync.cloudstorage_local import (
        is_macos,
        is_source_live,
        is_within_cloud_storage,
    )

    if not is_macos():
        return False, "unsupported"
    if not path or not is_within_cloud_storage(path):
        return False, "missing"
    from pathlib import Path as _P

    if not _P(path).is_dir():
        return False, "missing"
    if not is_source_live(path):
        return False, "offline"
    return True, "ok"


def _decode_nfs_subpaths(src: FolderSyncSource) -> list[str]:
    """Read the NFS row's selected subpaths.

    Prefer the new ``nfs_subpaths`` JSON column; fall back to the
    legacy single-string ``nfs_subpath`` so rows saved before the
    multi-select migration still render correctly. Always returns a
    canonical (deduped, no-overlap) list.
    """
    import json as _json
    from ...services.sync.nfs import canonicalise_subpaths

    raw = (src.nfs_subpaths or "").strip()
    if raw:
        try:
            decoded = _json.loads(raw)
            if isinstance(decoded, list):
                return canonicalise_subpaths([str(x) for x in decoded])
        except _json.JSONDecodeError:
            pass
    legacy = (src.nfs_subpath or "").strip()
    if legacy:
        return canonicalise_subpaths([legacy])
    return []


def _to_out(src: FolderSyncSource) -> SyncSourceOut:
    gh = None
    gd = None
    gdl = None
    nfs = None
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
            use_loopback=bool(src.gd_use_loopback),
            files_only=bool(src.gd_files_only),
        )
    elif src.source_type == "nfs":
        root, available, status_str = _nfs_status_snapshot()
        subpaths = _decode_nfs_subpaths(src)
        nfs = NfsSyncOut(
            # Legacy single-value echo: first element, or "" when the
            # root itself is the only selection. Old clients can keep
            # reading ``subpath`` until they migrate.
            subpath=subpaths[0] if subpaths else "",
            subpaths=subpaths,
            nfs_root=root,
            nfs_available=available,
            nfs_status=status_str,
        )
    sp = None
    tm = None
    if src.source_type == "sharepoint":
        sp = SharePointSyncOut(
            tenant_id=src.ms_tenant_id or "",
            client_id=src.ms_client_id or "",
            auth_method=src.ms_auth_method or "",
            sites=[SharePointSite(**s) for s in sp_coerce_sites(src.sp_selected_sites)],
            all_sites=bool(src.sp_all_sites),
            has_client_secret=bool(src.ms_client_secret),
            has_cert=bool(src.ms_cert_pem),
            use_loopback=bool(src.ms_use_loopback),
            connected=bool(src.ms_refresh_token),
        )
    elif src.source_type == "teams":
        tm = TeamsSyncOut(
            tenant_id=src.ms_tenant_id or "",
            client_id=src.ms_client_id or "",
            auth_method=src.ms_auth_method or "",
            user_mode=src.tm_user_mode or "me",
            user_id=src.tm_user_id or "",
            include_attended=bool(src.tm_include_attended),
            has_client_secret=bool(src.ms_client_secret),
            has_cert=bool(src.ms_cert_pem),
            use_loopback=bool(src.ms_use_loopback),
            connected=bool(src.ms_refresh_token),
        )
    if src.source_type == "google_drive_local":
        available, status_str = _gdl_status_snapshot(src.gdl_path or "")
        gdl = GoogleDriveLocalSyncOut(
            account=src.gdl_account or "",
            path=src.gdl_path or "",
            available=available,
            status=status_str,
        )
    return SyncSourceOut(
        folder_id=src.folder_id,
        source_type=src.source_type,
        sync_status=src.sync_status,
        sync_error=src.sync_error,
        last_synced_at=src.last_synced_at,
        auto_sync_enabled=bool(src.auto_sync_enabled),
        auto_sync_hours=int(src.auto_sync_hours),
        github=gh,
        google_drive=gd,
        google_drive_local=gdl,
        nfs=nfs,
        sharepoint=sp,
        teams=tm,
    )


def _check_access(
    folder_id: int, db: Session, user: CurrentUser
) -> Folder:
    """Read-level access check — for read-only endpoints (GET sync source)."""
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return folder


def _check_owner(
    folder_id: int, db: Session, user: CurrentUser
) -> Folder:
    """Owner-level access check — for any sync mutation.

    Sync configuration touches credentials, scheduling, and disk content;
    a read-only viewer of a shared folder must not be able to retrigger
    syncs or rotate auth.
    """
    folder = _check_access(folder_id, db, user)
    if not is_folder_owner(db, folder_id, user.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the folder owner can configure sync.",
        )
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
    folder = _check_owner(folder_id, db, user)
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
            _clear_microsoft_fields(src)
        src.source_type = "github"
        if existing is not None and existing.source_type != "github":
            src.nfs_subpath = None
            src.nfs_subpaths = None
        src.gh_repo = gh_cfg.repo.strip()
        src.gh_path = gh_cfg.path.strip("/")
        src.gh_branches = encode_branches_field(gh_cfg.branches)
        src.gh_all_branches = gh_cfg.all_branches
        src.gh_extended = gh_cfg.extended
        src.gh_auth_method = gh_cfg.auth_method or None
        src.gh_username = gh_cfg.username or None
        src.gh_pat = gh_cfg.pat or None
        src.gh_token = gh_cfg.ssh_key or None

    elif body.source_type == "nfs":
        if body.nfs is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Missing 'nfs' config for source_type='nfs'",
            )
        # NFS must be enabled + the configured root must be reachable.
        # Re-check at every save so an admin who removes the NFS root
        # immediately blocks new sync sources.
        root, available, status_str = _nfs_status_snapshot()
        if not available:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"NFS is not available ({status_str}); ask an admin to "
                "configure the NFS root.",
            )
        # Validate every chosen subpath: must resolve under the root,
        # no ``..``, no symlink escapes. Backwards-compat: if the
        # client only sent ``subpath`` (old single-value field), fold
        # it into the array.
        from ...services.sync.nfs import _resolve_under, canonicalise_subpaths

        raw_paths: list[str] = list(body.nfs.subpaths or [])
        if not raw_paths and (body.nfs.subpath or "").strip():
            raw_paths = [body.nfs.subpath]
        subpaths = canonicalise_subpaths(raw_paths)
        if not subpaths:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pick at least one folder under the NFS root before saving.",
            )
        for sp in subpaths:
            try:
                resolved = _resolve_under(Path(root), sp)
            except ValueError as e:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Invalid NFS subpath {sp!r}: {e}"
                ) from e
            if not resolved.exists() or not resolved.is_dir():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"NFS subpath does not exist or is not a directory: {sp!r}",
                )

        src = existing or FolderSyncSource(
            folder_id=folder_id, source_type="nfs"
        )
        if existing is not None and existing.source_type != "nfs":
            _clear_github_fields(src)
            _clear_google_drive_fields(src)
            _clear_microsoft_fields(src)
        src.source_type = "nfs"
        import json as _json
        src.nfs_subpaths = _json.dumps(subpaths)
        # Keep the legacy column populated with the first entry so old
        # clients reading the unmigrated DB still see *something*.
        src.nfs_subpath = subpaths[0] if subpaths else None

    elif body.source_type == "google_drive":
        if body.google_drive is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Missing 'google_drive' config for source_type='google_drive'",
            )
        gd_cfg = body.google_drive
        # ``has_client_secret`` is true when only the public client_id was
        # re-sent for an existing row (the secret stays masked client-side
        # and is only re-posted when the user types a new one). Same idea
        # applies to the service-account JSON: a re-save without retyping
        # the SA blob shouldn't lose the existing one.
        existing_has_secret = bool(existing and existing.gd_client_secret)
        existing_has_sa = bool(existing and existing.gd_service_account_json)
        has_oauth = bool(
            gd_cfg.client_id and (gd_cfg.client_secret or existing_has_secret)
        )
        has_sa = bool(gd_cfg.service_account_json or existing_has_sa)
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
            _clear_microsoft_fields(src)
            src.nfs_subpath = None
            src.nfs_subpaths = None
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
        # Loopback flag: switching it invalidates any refresh_token that
        # was issued under the other redirect URI (Google scopes refresh
        # tokens to the redirect URI used at consent), so drop it.
        prev_loopback = bool(existing and existing.gd_use_loopback)
        if prev_loopback != bool(gd_cfg.use_loopback):
            src.gd_refresh_token = None
        src.gd_use_loopback = bool(gd_cfg.use_loopback)
        src.gd_files_only = bool(gd_cfg.files_only)

    elif body.source_type in ("sharepoint", "teams"):
        src = _apply_microsoft_config(
            body=body, existing=existing, folder_id=folder_id
        )

    else:  # pragma: no cover — Pydantic Literal guards this
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported source_type: {body.source_type!r}",
        )

    # Auto-sync settings apply regardless of source type.
    src.auto_sync_enabled = bool(body.auto_sync_enabled)
    src.auto_sync_hours = int(body.auto_sync_hours)

    if existing is None:
        db.add(src)
    db.commit()
    db.refresh(src)
    if existing is None:
        # Toolbar visibility & label depend on has_sync_source — push the
        # update so the UI doesn't need to refetch the folder list.
        _publish_folder_changed(folder, has_sync_source=True)
    out = _to_out(src)
    # Push the full (secret-masked) config so an open sync modal in any tab
    # reflects the save live, with no post-save refetch. Secrets are already
    # reduced to has_* booleans by ``_to_out``.
    events.publish(
        "folders",
        {
            "type": "folder.sync_config_changed",
            "folder_id": folder_id,
            "config": out.model_dump(),
        },
    )
    return out


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


def _clear_microsoft_fields(src: FolderSyncSource) -> None:
    src.ms_tenant_id = None
    src.ms_client_id = None
    src.ms_client_secret = None
    src.ms_cert_pem = None
    src.ms_auth_method = None
    src.ms_refresh_token = None
    src.ms_use_loopback = False
    src.sp_selected_sites = None
    src.sp_all_sites = False
    src.tm_user_mode = None
    src.tm_user_id = None
    src.tm_include_attended = True


def _apply_microsoft_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
) -> FolderSyncSource:
    """Apply ``sharepoint`` / ``teams`` payload to a (new or existing) row.

    Shared auth fields land on ``ms_*``; per-connector specifics on
    ``sp_*`` / ``tm_*``. Switching from another source type clears the
    sibling credentials so we don't leak stale state across types.
    """
    is_sp = body.source_type == "sharepoint"
    cfg = body.sharepoint if is_sp else body.teams
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Missing '{body.source_type}' config",
        )
    if cfg.auth_method not in ("oauth", "app_secret", "app_cert"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick an auth method: 'oauth', 'app_secret', or 'app_cert'.",
        )
    if not cfg.tenant_id or not cfg.client_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "tenant_id and client_id are required.",
        )

    existing_has_secret = bool(existing and existing.ms_client_secret)
    existing_has_cert = bool(existing and existing.ms_cert_pem)
    if cfg.auth_method == "app_secret" and not (
        cfg.client_secret or existing_has_secret
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Auth method 'app_secret' needs a client_secret.",
        )
    if cfg.auth_method == "app_cert" and not (
        cfg.cert_pem or existing_has_cert
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Auth method 'app_cert' needs a PEM private key + certificate.",
        )
    if cfg.auth_method == "oauth" and not (
        cfg.client_secret or existing_has_secret
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Delegated OAuth needs a client_secret for token refresh.",
        )

    src = existing or FolderSyncSource(
        folder_id=folder_id, source_type=body.source_type
    )
    if existing is not None and existing.source_type != body.source_type:
        _clear_github_fields(src)
        _clear_google_drive_fields(src)
        if existing.source_type not in ("sharepoint", "teams"):
            _clear_microsoft_fields(src)
        src.nfs_subpath = None
    src.source_type = body.source_type

    # Shared MS fields.
    src.ms_tenant_id = cfg.tenant_id.strip()
    src.ms_client_id = cfg.client_id.strip()
    if cfg.client_secret:
        src.ms_client_secret = cfg.client_secret
    if cfg.cert_pem:
        src.ms_cert_pem = cfg.cert_pem
    prev_method = src.ms_auth_method
    src.ms_auth_method = cfg.auth_method
    # Auth-method changes invalidate the stored refresh_token (it was
    # issued under different scopes / client config).
    if prev_method and prev_method != cfg.auth_method:
        src.ms_refresh_token = None
    prev_loopback = bool(existing and existing.ms_use_loopback)
    if prev_loopback != bool(cfg.use_loopback):
        # Same story as gdrive — refresh tokens are bound to the
        # redirect URI used at consent. Flipping loopback rotates that
        # URI, so the existing token is useless.
        src.ms_refresh_token = None
    src.ms_use_loopback = bool(cfg.use_loopback)

    if is_sp:
        src.sp_selected_sites = sp_encode_sites(
            [{"id": s.id, "displayName": s.displayName, "webUrl": s.webUrl}
             for s in cfg.sites]
        )
        src.sp_all_sites = bool(cfg.all_sites)
        # Clear teams-specific fields in case we're switching from teams.
        src.tm_user_mode = None
        src.tm_user_id = None
    else:
        src.tm_user_mode = cfg.user_mode
        src.tm_user_id = cfg.user_id.strip() or None
        src.tm_include_attended = bool(cfg.include_attended)
        src.sp_selected_sites = None
        src.sp_all_sites = False

    return src


def _clear_google_drive_fields(src: FolderSyncSource) -> None:
    src.gd_client_id = None
    src.gd_client_secret = None
    src.gd_refresh_token = None
    src.gd_service_account_json = None
    src.gd_folder_id = None
    src.gd_use_loopback = False
    src.gd_files_only = False


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
    folder = _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        return
    db.delete(src)
    db.commit()
    _publish_folder_changed(folder, has_sync_source=False)
    # config=None tells the client to drop any cached config for this folder.
    events.publish(
        "folders",
        {
            "type": "folder.sync_config_changed",
            "folder_id": folder_id,
            "config": None,
        },
    )


@router.delete("/error", response_model=SyncSourceOut)
def clear_sync_error(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncSourceOut:
    """Wipe ``sync_error`` and reset ``sync_status`` from 'error' to 'idle'.

    The error message is informational once the user has read it; we
    don't want a stale 'API not enabled' bullet from yesterday's run
    polluting the modal after the admin actually enabled the API.

    Idempotent: noop when there's no error to clear, no-op when there's
    no source row for this folder. Either way returns the (possibly-
    unchanged) source so the SPA can re-render from one response.
    """
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No sync source for this folder")
    if src.sync_error is not None or src.sync_status == "error":
        src.sync_error = None
        if src.sync_status == "error":
            src.sync_status = "idle"
        db.commit()
        db.refresh(src)
        events.publish(
            "folders",
            {
                "type": "folder.sync_source_changed",
                "folder_id": folder_id,
                "sync_status": src.sync_status,
                "sync_error": src.sync_error,
                "last_synced_at": src.last_synced_at,
            },
        )
    return _to_out(src)


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
    _check_owner(folder_id, db, user)
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
    if src.source_type == "nfs":
        root, available, status_str = _nfs_status_snapshot()
        if not available:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"NFS is not available ({status_str})",
            )
        if not _decode_nfs_subpaths(src):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pick at least one folder under the NFS root before syncing.",
            )
    if src.source_type == "sharepoint":
        if not (src.sp_all_sites or sp_coerce_sites(src.sp_selected_sites)):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pick at least one SharePoint site (or enable 'all sites').",
            )
    if src.source_type == "teams":
        mode = src.tm_user_mode or "me"
        if mode == "specific" and not src.tm_user_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Pick a user for Teams 'specific user' mode before syncing.",
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
    _check_owner(folder_id, db, user)
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


# Port used by the optional localhost-loopback redirect URI. Hardcoded
# on purpose — admins register this exact URL in GCP, and a small
# nginx bridge on their machine listens on this port and proxies the
# callback back to this server. Keep in sync with the bridge config.
GD_LOOPBACK_PORT = 53682
GD_LOOPBACK_REDIRECT_URI = (
    f"http://localhost:{GD_LOOPBACK_PORT}/api/sync/oauth/google/callback"
)


def _oauth_redirect_uri(request: Request, *, use_loopback: bool = False) -> str:
    """Build the redirect URI the way Google's consent screen will see it.

    When ``use_loopback`` is set (per-folder opt-in) we return the
    fixed localhost URL — the admin has registered that in GCP and is
    running a local nginx bridge that proxies the callback back here.

    Otherwise: ``request.base_url`` reflects what the ASGI server received
    on the wire, which is plain HTTP when a reverse proxy (Cloudflare,
    Caddy, nginx) terminates TLS in front of the app. Google then rejects
    the code-exchange with ``redirect_uri_mismatch`` because the value we
    registered is ``https://…``. Read ``X-Forwarded-Proto`` and
    ``X-Forwarded-Host`` ourselves so this works without requiring uvicorn
    to be launched with ``--proxy-headers``.

    Falls back to ``request.url`` for the localhost / no-proxy case.
    """
    if use_loopback:
        return GD_LOOPBACK_REDIRECT_URI
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
    _check_owner(folder_id, db, user)
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
    _check_owner(folder_id, db, user)
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
    try:
        data = await gd_list_root_folders(
            client_id=src.gd_client_id or "",
            client_secret=src.gd_client_secret or "",
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
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Drive source")
    if not src.gd_refresh_token and not src.gd_service_account_json:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not connected to Google Drive")
    try:
        children = await gd_list_folder_children(
            parent_id=parent_id,
            drive_id=drive_id,
            client_id=src.gd_client_id or "",
            client_secret=src.gd_client_secret or "",
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
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type != "google_drive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Drive source")
    if not src.gd_refresh_token and not src.gd_service_account_json:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect via OAuth or save a service-account JSON before testing APIs",
        )
    auth = GoogleDriveAuth(
        client_id=src.gd_client_id or "",
        client_secret=src.gd_client_secret or "",
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


# ---------------------------------------------------------------------------
# NFS — capability check + directory picker (folder-agnostic).
#
# Mounted on ``oauth_router`` because these endpoints don't carry a
# ``folder_id`` in their URLs. The sync UI hits ``/sync/nfs/status`` once
# to decide whether to surface NFS as an option, then ``/sync/nfs/browse``
# to step into the picker.
# ---------------------------------------------------------------------------


class NfsCapabilityOut(BaseModel):
    available: bool
    status: str  # 'disabled' | 'ok' | 'missing' | 'not_a_directory' | 'unreadable'
    nfs_root: str  # echo so the modal can show "rooted at ..."


class NfsBrowseEntry(BaseModel):
    name: str
    rel_path: str


class NfsBrowseOut(BaseModel):
    rel_path: str       # the directory we listed
    parent: str | None  # one level up, or None at the root
    entries: list[NfsBrowseEntry]


@oauth_router.get("/nfs/status", response_model=NfsCapabilityOut)
def nfs_status(_: CurrentUser = Depends(current_user)) -> NfsCapabilityOut:
    """Tell the SPA whether the NFS connector is currently usable.

    Cheap probe (single stat + iterdir) so the sync modal can call it
    on open without measurable cost. Read-level access is enough — any
    signed-in user can discover whether NFS is on; only owners can
    actually configure it.
    """
    root, available, status_str = _nfs_status_snapshot()
    return NfsCapabilityOut(available=available, status=status_str, nfs_root=root)


@oauth_router.get("/nfs/browse", response_model=NfsBrowseOut)
def nfs_browse(
    rel: str = Query("", description="POSIX path relative to the admin NFS root; '' = root"),
    user: CurrentUser = Depends(current_user),
) -> NfsBrowseOut:
    """Return the immediate subdirectories of ``<nfs_root>/<rel>``.

    Files are intentionally omitted — the picker walks directories only,
    and any leaf you'd want to index is reachable by descending into its
    parent. Path safety is enforced by ``services.sync.nfs._resolve_under``;
    ``..``-escapes and symlink redirections to outside the root raise 400.
    """
    from ...services.sync.nfs import list_children

    root, available, status_str = _nfs_status_snapshot()
    if not available:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"NFS is not available ({status_str})",
        )
    rel = (rel or "").strip().strip("/")
    try:
        entries = list_children(rel)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except (PermissionError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    parent = None
    if rel:
        parts = rel.split("/")
        parent = "/".join(parts[:-1])  # may be ""; "" → root, frontend handles it
    return NfsBrowseOut(
        rel_path=rel,
        parent=parent,
        entries=[NfsBrowseEntry(**e) for e in entries],
    )


# ---------------------------------------------------------------------------
# Google Drive — LOCAL (desktop, no credentials).
#
# Rides on the already-installed Google Drive for Desktop app. We enumerate the
# signed-in accounts under ~/Library/CloudStorage, let the user browse the
# (free, stub) tree, and register the chosen subtree as an indexed-in-place
# folder. We NEVER download via API or write into the Drive.
# ---------------------------------------------------------------------------


class GdlAccountOut(BaseModel):
    email: str
    path: str
    provider: str


class GdlAccountsOut(BaseModel):
    available: bool          # macOS + Drive app running
    status: str              # "ok" | "unsupported" | "offline"
    accounts: list[GdlAccountOut]


class GdlBrowseEntry(BaseModel):
    name: str
    path: str                # absolute path under CloudStorage
    is_dir: bool
    is_native_doc: bool
    is_stub: bool
    size: int


class GdlBrowseOut(BaseModel):
    path: str
    parent: str | None
    entries: list[GdlBrowseEntry]


@oauth_router.get("/local/accounts", response_model=GdlAccountsOut)
def gdl_accounts(_: CurrentUser = Depends(current_user)) -> GdlAccountsOut:
    """List signed-in Google Drive for Desktop accounts on this machine.

    Cheap (one directory listing); the sync modal calls it on open. Returns
    ``unsupported`` off macOS and ``offline`` when the Drive app isn't running.
    """
    from ...services.sync.cloudstorage_local import (
        is_drive_app_running,
        is_macos,
        list_accounts,
    )

    if not is_macos():
        return GdlAccountsOut(available=False, status="unsupported", accounts=[])
    accounts = [GdlAccountOut(**a.as_dict()) for a in list_accounts()]
    running = is_drive_app_running()
    status_str = "ok" if running else "offline"
    return GdlAccountsOut(
        available=running and bool(accounts), status=status_str, accounts=accounts
    )


@oauth_router.get("/local/browse", response_model=GdlBrowseOut)
def gdl_browse(
    path: str = Query(..., description="Absolute path under ~/Library/CloudStorage"),
    user: CurrentUser = Depends(current_user),
) -> GdlBrowseOut:
    """List the children of a directory in the local Google Drive mount.

    Free + read-only (``scandir``/``stat`` only — never opens content, so
    nothing downloads). Path-safety: must resolve under ~/Library/CloudStorage,
    enforced by ``cloudstorage_local.browse``.
    """
    from ...services.sync.cloudstorage_local import (
        CLOUD_STORAGE_ROOT,
        browse,
        is_within_cloud_storage,
    )

    try:
        entries = browse(path)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except OSError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e

    from pathlib import Path as _P

    p = _P(path).resolve(strict=False)
    parent = None
    # Offer a parent link as long as it stays within CloudStorage.
    cand = p.parent
    if cand != p and is_within_cloud_storage(cand) and cand != CLOUD_STORAGE_ROOT.resolve(strict=False):
        parent = str(cand)
    return GdlBrowseOut(
        path=str(p),
        parent=parent,
        entries=[GdlBrowseEntry(**e.as_dict()) for e in entries],
    )


class GdlConnectIn(BaseModel):
    folder_id: int                     # the folder being configured (the opened one)
    account: str                       # signed-in email (provenance only)
    path: str                          # chosen Drive folder under CloudStorage
    auto_sync_enabled: bool = False
    auto_sync_hours: int = Field(default=6, ge=1, le=24)


@oauth_router.post("/local/connect", response_model=SyncTriggerOut)
def gdl_connect(
    body: GdlConnectIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncTriggerOut:
    """Point an EXISTING folder at a Google Drive folder (index in place).

    Like every other connector, this configures the folder whose sync dialog
    was opened — it does NOT create new folders. The folder's ``path`` is
    repointed at the chosen Drive subtree (we index in place; content
    materializes lazily as the extract worker reads each file), its
    ``source_type`` becomes ``google_drive_local``, and a matching sync source
    is upserted. The initial sync is then enqueued.
    """
    from sqlalchemy import select as _select

    from ...services.sync.cloudstorage_local import (
        is_macos,
        is_source_live,
        is_within_cloud_storage,
    )

    _check_owner(body.folder_id, db, user)
    folder = db.get(Folder, body.folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    if not is_macos():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Local Google Drive sync is only available on the macOS desktop app.",
        )
    path = (body.path or "").strip()
    if not path or not is_within_cloud_storage(path):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick a folder inside your Google Drive (~/Library/CloudStorage).",
        )
    if not is_source_live(path):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google Drive isn't available right now (app not running or folder "
            "empty). Start Google Drive for Desktop and try again.",
        )

    from pathlib import Path as _P

    abs_path = str(_P(path).resolve(strict=False))
    # Refuse if ANOTHER folder already points at this Drive path.
    clash = db.execute(
        _select(Folder).where(Folder.path == abs_path, Folder.id != folder.id)
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Another folder ('{clash.display_name}', id={clash.id}) already "
            f"indexes that Drive folder.",
        )

    # Repoint the opened folder AT the Drive subtree (index in place).
    folder.path = abs_path
    folder.source_type = "google_drive_local"

    existing = db.get(FolderSyncSource, folder.id)
    src = existing or FolderSyncSource(
        folder_id=folder.id, source_type="google_drive_local"
    )
    src.source_type = "google_drive_local"
    src.gdl_account = (body.account or "").strip()
    src.gdl_path = abs_path
    src.auto_sync_enabled = bool(body.auto_sync_enabled)
    src.auto_sync_hours = int(body.auto_sync_hours)
    src.sync_status = "idle"
    src.sync_error = None
    if existing is None:
        db.add(src)
    db.flush()

    # Initial sync (builds the provenance sidecar under data_dir, then the
    # post-sync rescan enqueues extracts). We deliberately do NOT start the
    # filesystem watcher: FSEvents is unreliable on File Provider mounts, so
    # refresh is driven by (auto-)sync rescans instead.
    job_id = job_queue.enqueue(
        db, "sync", {"folder_id": folder.id}, dedup_key=f"sync:{folder.id}"
    )
    db.commit()
    events.publish(
        "folders", {"type": "folder.sync_source_changed", "folder_id": folder.id}
    )
    return SyncTriggerOut(folder_id=folder.id, job_id=job_id)


# ---------------------------------------------------------------------------
# Microsoft (SharePoint + Teams) — shared auth + scope-check endpoints.
#
# Both connectors share the same OAuth client config and the same callback
# URL — only the saved row tells us afterwards which source_type to land on.
# ---------------------------------------------------------------------------


class MsAuthInitOut(BaseModel):
    auth_url: str


# Same loopback story as Google: the admin can register only a localhost
# URL in Azure AD and run a small nginx bridge that forwards back here.
MS_LOOPBACK_PORT = 53682
MS_LOOPBACK_REDIRECT_URI = (
    f"http://localhost:{MS_LOOPBACK_PORT}/api/sync/oauth/microsoft/callback"
)


def _ms_redirect_uri(request: Request, *, use_loopback: bool = False) -> str:
    if use_loopback:
        return MS_LOOPBACK_REDIRECT_URI
    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    proto = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{proto}://{host}/api/sync/oauth/microsoft/callback"


def _build_ms_auth_for_row(src: FolderSyncSource) -> msa.MicrosoftAuth:
    return msa.MicrosoftAuth(
        tenant_id=src.ms_tenant_id or "",
        client_id=src.ms_client_id or "",
        client_secret=src.ms_client_secret or "",
        cert_pem=src.ms_cert_pem or "",
        refresh_token=src.ms_refresh_token or "",
        method=src.ms_auth_method or "",
    )


@router.post("/microsoft/auth", response_model=MsAuthInitOut)
def ms_auth_init(
    folder_id: int,
    request: Request,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsAuthInitOut:
    """Build the Microsoft OAuth URL the UI should pop open."""
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Configure SharePoint or Teams (tenant/client_id/secret) before connecting",
        )
    if not (src.ms_tenant_id and src.ms_client_id and src.ms_client_secret):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Save tenant_id, client_id and client_secret before connecting.",
        )
    state = base64.urlsafe_b64encode(str(folder_id).encode()).decode()
    auth_url = msa.get_auth_url(
        tenant_id=src.ms_tenant_id,
        client_id=src.ms_client_id,
        redirect_uri=_ms_redirect_uri(request, use_loopback=bool(src.ms_use_loopback)),
        state=state,
    )
    return MsAuthInitOut(auth_url=auth_url)


@oauth_router.get("/oauth/microsoft/callback")
async def ms_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    admin_consent: str | None = Query(None),
    tenant: str | None = Query(None),
) -> HTMLResponse:
    """Microsoft OAuth callback — works for both SharePoint and Teams rows.

    Microsoft can redirect back with ``?error=...&error_description=...``
    instead of ``?code=...`` (admin-consent missing, scope typo,
    redirect-uri mismatch). We surface that verbatim so the admin can
    fix the real issue instead of staring at a 422 schema error from
    a missing ``code`` query param.
    """
    # Admin-consent return — the /adminconsent endpoint redirects here
    # with ``admin_consent=True&tenant=…`` and no ``code`` or ``state``.
    # Tenant-wide grant is already saved server-side at Azure AD by the
    # time the redirect fires; nothing for us to do here except show a
    # friendly page so the admin doesn't think it failed.
    if (admin_consent or "").lower() == "true":
        logger.info("Microsoft admin consent recorded for tenant=%s", tenant)
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:20px;'>"
            "<h3>Admin consent recorded ✓</h3>"
            "<p>Tenant-wide permissions are now granted. You can close "
            "this tab — the user who was setting up the integration can "
            "click Connect again and sign in normally.</p>"
            "</body></html>"
        )
    if error:
        logger.warning(
            "Microsoft OAuth callback returned error=%s desc=%s",
            error, error_description,
        )
        body = (
            f"<html><body style='font-family:sans-serif;padding:20px;'>"
            f"<h3>Microsoft sign-in failed</h3>"
            f"<p><strong>{error}</strong></p>"
            f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:12px;'>"
            f"{(error_description or '').strip()}</pre>"
            f"<p>Close this tab and adjust the Azure AD app, then try again.</p>"
            f"</body></html>"
        )
        return HTMLResponse(body, status_code=400)
    if not code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft callback did not include code or error",
        )
    if not state:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft callback did not include state",
        )
    try:
        folder_id = int(base64.urlsafe_b64decode(state.encode()).decode())
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid state parameter"
        ) from e

    from ...db.database import session_scope

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is None or src.source_type not in ("sharepoint", "teams"):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Microsoft source not found for this state",
            )
        tenant_id = src.ms_tenant_id or ""
        client_id = src.ms_client_id or ""
        client_secret = src.ms_client_secret or ""
        use_loopback = bool(src.ms_use_loopback)
        source_type = src.source_type

    try:
        tokens = await msa.exchange_code_for_tokens(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=_ms_redirect_uri(request, use_loopback=use_loopback),
        )
    except Exception as e:
        logger.exception("Microsoft OAuth callback failed for folder %s", folder_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Microsoft did not return a refresh_token. Make sure 'offline_access' "
            "is in the requested scopes and try again.",
        )

    with session_scope() as s:
        src = s.get(FolderSyncSource, folder_id)
        if src is not None:
            src.ms_refresh_token = refresh_token

    events.publish(
        "folders",
        {
            "type": "folder.ms_connected",
            "folder_id": folder_id,
            "source_type": source_type,
        },
    )

    return HTMLResponse(
        "<html><body><script>window.close()</script>"
        "<p>Microsoft account connected. You can close this tab.</p></body></html>"
    )


class MsSitesOut(BaseModel):
    sites: list[SharePointSite]


@router.get("/microsoft/sites", response_model=MsSitesOut)
async def ms_list_sites(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsSitesOut:
    """List sites the credentials can see (for the picker)."""
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Microsoft source")
    auth = _build_ms_auth_for_row(src)
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect (or configure app-only creds) before listing sites.",
        )
    try:
        sites = await sp_list_all_sites(auth)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    # Persist the rotated refresh token, if any.
    if auth.rotated_refresh_token:
        src.ms_refresh_token = auth.rotated_refresh_token
        db.commit()
    return MsSitesOut(sites=[SharePointSite(**s) for s in sites])


class MsUser(BaseModel):
    id: str
    displayName: str = ""
    userPrincipalName: str = ""
    mail: str = ""


class MsUsersOut(BaseModel):
    users: list[MsUser]


@router.get("/microsoft/users", response_model=MsUsersOut)
async def ms_list_users(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsUsersOut:
    """Tenant user picker for Teams 'specific user' mode."""
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Microsoft source")
    auth = _build_ms_auth_for_row(src)
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect (or configure app-only creds) before listing users.",
        )
    try:
        users = await tm_list_tenant_users(auth)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if auth.rotated_refresh_token:
        src.ms_refresh_token = auth.rotated_refresh_token
        db.commit()
    return MsUsersOut(users=[MsUser(**u) for u in users])


class MsScopeMissing(BaseModel):
    feature: str
    scope: str
    impact: str


class MsScopeCheckOut(BaseModel):
    granted: list[str]
    missing: list[MsScopeMissing]
    app_only: bool


@router.get("/microsoft/scope-check", response_model=MsScopeCheckOut)
async def ms_scope_check(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MsScopeCheckOut:
    """Mint a fresh token and compare its claims against required scopes.

    Drives the yellow "missing scope" callout on the sync config page.
    """
    _check_owner(folder_id, db, user)
    src = db.get(FolderSyncSource, folder_id)
    if src is None or src.source_type not in ("sharepoint", "teams"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Microsoft source")
    auth = _build_ms_auth_for_row(src)
    if not auth.configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Connect (or configure app-only creds) before checking scopes.",
        )
    app_only = auth.method in ("app_secret", "app_cert")
    try:
        if app_only:
            token = await msa.get_app_only_token(auth)
        else:
            token = await msa.refresh_access_token(auth)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if auth.rotated_refresh_token:
        src.ms_refresh_token = auth.rotated_refresh_token
        db.commit()
    report = msa.compute_missing_scopes(token, app_only=app_only)
    return MsScopeCheckOut(
        granted=report["granted"],
        missing=[MsScopeMissing(**m) for m in report["missing"]],
        app_only=app_only,
    )
