"""Google Drive — LOCAL (desktop, no credentials).

Rides on the already-installed Google Drive for Desktop app. We enumerate the
signed-in accounts under ~/Library/CloudStorage, let the user browse the
(free, stub) tree, and register the chosen subtree as an indexed-in-place
folder. We NEVER download via API or write into the Drive.

``google_drive_local`` rows are created by their own connect endpoint, not
the generic PUT envelope — the handler registered here has no ``apply``.
"""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....db.models import Folder, FolderSyncSource
from ....services import events, job_queue
from ....services.acl import CurrentUser
from ...deps import current_user, db_session
from . import registry
from .base import SyncTriggerOut, check_owner, oauth_router, publish_sync_config_changed


class GoogleDriveLocalSyncOut(BaseModel):
    """Status for ``source_type == 'google_drive_local'`` (desktop, no creds)."""

    account: str       # signed-in Google account email
    path: str          # chosen subtree under ~/Library/CloudStorage
    paths: list[str] = []  # ALL selected subtrees — the dialog pre-checks these
    available: bool    # macOS + Drive app running + mount live right now
    status: str        # "ok" | "offline" | "missing" | "unsupported"


def _status_snapshot(path: str) -> tuple[bool, str]:
    """Return ``(available, status)`` for a google_drive_local source.

    Re-checked on every render so an offline Drive app / signed-out account
    shows as unavailable without a restart.
    """
    from pathlib import Path

    from ....services.sync.cloudstorage_local import (
        is_macos,
        is_source_live,
        is_within_cloud_storage,
    )

    if not is_macos():
        return False, "unsupported"
    if not path or not is_within_cloud_storage(path):
        return False, "missing"
    if not Path(path).is_dir():
        return False, "missing"
    if not is_source_live(path):
        return False, "offline"
    return True, "ok"


def build_out(src: FolderSyncSource) -> GoogleDriveLocalSyncOut:
    available, status_str = _status_snapshot(src.gdl_path or "")
    # Same precedence as CloudLocalConnector.resolve_config: the JSON
    # list when present, else the legacy single path.
    paths: list[str] = []
    raw = (src.gdl_paths or "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                paths = [str(x) for x in decoded if x]
        except (ValueError, TypeError):
            paths = []
    if not paths and src.gdl_path:
        paths = [src.gdl_path]
    return GoogleDriveLocalSyncOut(
        account=src.gdl_account or "",
        path=src.gdl_path or "",
        paths=paths,
        available=available,
        status=status_str,
    )


registry.register(
    registry.SourceHandler(
        source_type="google_drive_local",
        out_field="google_drive_local",
        build_out=build_out,
    )
)


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
    from ....services.sync.cloudstorage_local import (
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
    from pathlib import Path

    from ....services.sync.cloudstorage_local import (
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

    p = Path(path).resolve(strict=False)
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
    account_root: str                  # the account MOUNT root → becomes folder.path
    paths: list[str]                   # selected subtree absolute paths under the mount
    auto_sync_enabled: bool = False
    auto_sync_hours: int = Field(default=6, ge=1, le=24)


@oauth_router.post("/local/connect", response_model=SyncTriggerOut)
def gdl_connect(
    body: GdlConnectIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncTriggerOut:
    """Configure the OPENED folder to mirror selected Google Drive folders.

    Sets the folder's ``path`` to the account mount root and records the
    selected subtree paths. The scanner enumerates only those subtrees but
    numbers each file's rel_path relative to the mount, so the Drive's folder
    structure (incl. synthesised parent dirs) is mirrored under this folder.
    Read-only / index-in-place: content materializes lazily as files are read.
    Configures the opened folder — it does NOT create new folders.
    """
    from pathlib import Path

    from ....services.sync.cloudstorage_local import (
        is_macos,
        is_source_live,
        is_within_cloud_storage,
    )
    from .core import to_out

    check_owner(body.folder_id, db, user)
    folder = db.get(Folder, body.folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    if not is_macos():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Local Google Drive sync is only available on the macOS desktop app.",
        )

    mount = str(Path((body.account_root or "").strip()).resolve(strict=False))
    if not mount or not is_within_cloud_storage(mount):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid Google Drive account (mount not under ~/Library/CloudStorage).",
        )
    if not is_source_live(mount):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google Drive isn't available right now (app not running). "
            "Start Google Drive for Desktop and try again.",
        )

    # Validate each selected subtree: under the mount + a real directory.
    sel: list[str] = []
    for raw in body.paths or []:
        p = str(Path((raw or "").strip()).resolve(strict=False))
        if not p:
            continue
        if not (p == mount or p.startswith(mount + "/")):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Selected folder is outside the account mount: {raw}",
            )
        if not Path(p).is_dir():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Not a folder: {raw}"
            )
        sel.append(p)
    if not sel:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one Google Drive folder to index.",
        )
    # Drop any selection that's already covered by an ancestor selection
    # (indexing is recursive), so we never double-walk a subtree.
    sel = sorted(set(sel))
    canonical = [
        p for p in sel
        if not any(p != q and p.startswith(q + "/") for q in sel)
    ]

    # Configure the opened folder: path = the mount root (rel-path base).
    folder.path = mount
    folder.source_type = "google_drive_local"

    existing = db.get(FolderSyncSource, folder.id)
    src = existing or FolderSyncSource(
        folder_id=folder.id, source_type="google_drive_local"
    )
    src.source_type = "google_drive_local"
    src.gdl_account = (body.account or "").strip()
    src.gdl_path = canonical[0]                 # legacy single-path fallback
    src.gdl_paths = json.dumps(canonical)       # the real selection
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
    db.refresh(src)
    # Same live-update contract as the PUT route: the modal's per-folder
    # config cache is only as fresh as this event. Skipping it here is what
    # made reopening the dialog show an empty form after a Drive connect.
    publish_sync_config_changed(folder.id, to_out(src))
    events.publish(
        "folders",
        {
            "type": "folder.sync_source_changed",
            "folder_id": folder.id,
            "sync_status": src.sync_status,
            "sync_error": src.sync_error,
            "last_synced_at": src.last_synced_at,
        },
    )
    return SyncTriggerOut(folder_id=folder.id, job_id=job_id)
