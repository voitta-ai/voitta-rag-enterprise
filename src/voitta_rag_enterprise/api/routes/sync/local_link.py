"""Linked folder — a host directory indexed IN PLACE (nothing copied).

Shape borrowed from two neighbours: the admin-set root + capability probe +
scoped directory browse of the NFS module, and the lifecycle of the
Google-Drive-local module — rows are created by their own connect endpoint,
not the PUT envelope, because linking must re-point ``folder.path`` (the
envelope's ``apply`` only sees the sync row). The rules every in-place folder
then obeys live in ``services.in_place``.

One folder links exactly one directory. To index two subtrees, link two
folders.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....config import get_settings
from ....db.models import Folder, FolderSyncSource
from ....services import events, job_queue
from ....services.acl import CurrentUser
from ....services.ignore import normalize_patterns, parse_patterns
from ....services.in_place import indexes_in_place
from ....services.sync.localfs import list_children, resolve_under
from ....services.watcher import unwatch_folder_in_default
from ...deps import current_user, db_session
from . import registry
from .base import (
    SyncTriggerOut,
    check_owner,
    folder_has_real_files,
    oauth_router,
    publish_folder_changed,
    publish_sync_config_changed,
)

logger = logging.getLogger(__name__)

ROOT_LABEL = "linked-folder root"

# Sidecars a managed folder may carry while still counting as "empty"
# (folder_has_real_files ignores them); removed with the managed directory.
_MANAGED_SIDECARS = (".voitta_sources.json", ".voitta_timestamps.json")


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _probe_dir(p: Path) -> tuple[bool, str]:
    if not p.exists():
        return False, "missing"
    if not p.is_dir():
        return False, "not_a_directory"
    if not os.access(p, os.R_OK | os.X_OK):
        return False, "unreadable"
    return True, "ok"


def root_snapshot() -> tuple[str, bool, str]:
    """``(link_root, available, status)`` — re-checked on every call so a
    root that disappears flips the feature off without a restart."""
    from ....services.admin_store import get_link_root

    root = get_link_root()
    if not root:
        return "", False, "disabled"
    ok, status_str = _probe_dir(Path(root))
    return root, ok, status_str


def _rel_to_root(path: str, root: str) -> str:
    """The linked path relative to the CURRENT admin root, or "" when the
    root changed underneath (the link keeps working; only the picker loses
    its anchor)."""
    if not root:
        return ""
    try:
        return (
            Path(path).resolve(strict=False)
            .relative_to(Path(root).resolve(strict=False))
            .as_posix()
        )
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Handler (GET /folders/{id}/sync block + trigger readiness)
# ---------------------------------------------------------------------------


class LocalLinkSyncOut(BaseModel):
    path: str            # absolute linked directory (== folder.path)
    rel_path: str        # relative to the admin root; "" if the root moved
    ignore: list[str]    # this folder's extra ignore patterns
    link_root: str       # current admin root, for the picker
    available: bool      # the linked directory exists and is readable now
    status: str          # "ok" | "missing" | "not_a_directory" | "unreadable"


def build_out(src: FolderSyncSource) -> LocalLinkSyncOut:
    path = src.ll_path or ""
    available, status_str = _probe_dir(Path(path)) if path else (False, "missing")
    root, _root_ok, _root_status = root_snapshot()
    return LocalLinkSyncOut(
        path=path,
        rel_path=_rel_to_root(path, root),
        ignore=parse_patterns(src.ll_ignore),
        link_root=root,
        available=available,
        status=status_str,
    )


def trigger_check(src: FolderSyncSource) -> None:
    ok, status_str = _probe_dir(Path(src.ll_path or ""))
    if not ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Linked directory is {status_str}: {src.ll_path or '(unset)'}",
        )


registry.register(
    registry.SourceHandler(
        source_type="local_link",
        out_field="local_link",
        build_out=build_out,
        trigger_check=trigger_check,
    )
)


# ---------------------------------------------------------------------------
# Capability probe + directory browse (folder-agnostic, on oauth_router)
# ---------------------------------------------------------------------------


class LinkCapabilityOut(BaseModel):
    available: bool
    status: str  # 'disabled' | 'ok' | 'missing' | 'not_a_directory' | 'unreadable'
    link_root: str


class LinkBrowseEntry(BaseModel):
    name: str
    rel_path: str


class LinkBrowseOut(BaseModel):
    rel_path: str        # the directory we listed, relative to the root
    path: str            # its absolute path, for display
    parent: str | None   # one level up, or None at the root
    entries: list[LinkBrowseEntry]


@oauth_router.get("/link/status", response_model=LinkCapabilityOut)
def link_status(_: CurrentUser = Depends(current_user)) -> LinkCapabilityOut:
    """Whether "Linked folder" is currently offered as a sync source."""
    root, available, status_str = root_snapshot()
    return LinkCapabilityOut(available=available, status=status_str, link_root=root)


@oauth_router.get("/link/browse", response_model=LinkBrowseOut)
def link_browse(
    rel: str = Query("", description="POSIX path relative to the linked-folder root; '' = root"),
    _: CurrentUser = Depends(current_user),
) -> LinkBrowseOut:
    """Immediate subdirectories of ``<link_root>/<rel>`` — directories only."""
    root, available, status_str = root_snapshot()
    if not available:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Linked folders are not available ({status_str})",
        )
    rel = (rel or "").strip().strip("/")
    try:
        entries = list_children(Path(root), rel, root_label=ROOT_LABEL)
        listed = resolve_under(Path(root), rel, root_label=ROOT_LABEL)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except (PermissionError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    parent = None
    if rel:
        parent = "/".join(rel.split("/")[:-1])  # "" at depth 1 → the root
    return LinkBrowseOut(
        rel_path=rel,
        path=str(listed),
        parent=parent,
        entries=[LinkBrowseEntry(**e) for e in entries],
    )


# ---------------------------------------------------------------------------
# Connect: turn the opened (empty) folder into a linked folder
# ---------------------------------------------------------------------------


class LinkConnectIn(BaseModel):
    folder_id: int
    rel_path: str = Field(description="Directory to link, relative to the linked-folder root")
    ignore: list[str] = Field(default_factory=list)
    auto_sync_enabled: bool = True
    auto_sync_hours: int = Field(default=1, ge=1, le=24)


def _remove_managed_dir(managed: Path) -> None:
    """Remove the empty directory ``create_folder`` made under VOITTA_ROOT_PATH.

    Only when it genuinely lives under the root (a hand-edited row pointing
    elsewhere is left alone) and only its own sidecars are inside — the caller
    has already established there are no real files. Anything else is a bug
    we want to hear about, not paper over.
    """
    root = get_settings().root_path
    if root is None or not managed.exists():
        return
    try:
        if not managed.resolve().is_relative_to(Path(root).expanduser().resolve()):
            logger.warning("link: %s is outside VOITTA_ROOT_PATH; leaving it", managed)
            return
    except OSError:
        return
    for name in _MANAGED_SIDECARS:
        (managed / name).unlink(missing_ok=True)
    try:
        managed.rmdir()
    except OSError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Could not remove the empty managed directory {managed}: {e}",
        ) from e


@oauth_router.post("/link/connect", response_model=SyncTriggerOut)
def link_connect(
    body: LinkConnectIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SyncTriggerOut:
    """Point the OPENED folder at a directory under the linked-folder root.

    The folder must be empty (nothing indexed, nothing on disk beyond sync
    sidecars): its empty managed directory is removed, its watch dropped, and
    ``folder.path`` becomes the linked directory. Re-connecting an already
    linked folder re-points it and/or replaces its ignore list. A first
    rescan is enqueued; nothing is copied and nothing is written into the
    linked tree.
    """
    from .core import to_out

    folder = check_owner(body.folder_id, db, user)

    root, available, status_str = root_snapshot()
    if not available:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Linked folders are not available ({status_str}); ask an admin to set "
            "the linked-folder root.",
        )

    rel = (body.rel_path or "").strip().strip("/")
    if not rel:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick a directory under the linked-folder root — the root itself cannot be linked.",
        )
    try:
        target = resolve_under(Path(root), rel, root_label=ROOT_LABEL)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid path {rel!r}: {e}") from e
    ok, why = _probe_dir(target)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot link {rel!r}: {why}")

    try:
        ignore = normalize_patterns(body.ignore)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    existing = db.get(FolderSyncSource, folder.id)
    if existing is not None and existing.source_type != "local_link":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This folder already has a different sync source; use another folder.",
        )
    dup = db.execute(
        select(Folder).where(Folder.path == str(target), Folder.id != folder.id)
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"That directory is already linked by folder {dup.id} ({dup.display_name}).",
        )

    if not indexes_in_place(folder):
        # Converting a managed upload folder: it must start empty, and its
        # managed directory + watch go away — from here on the folder IS the
        # linked directory.
        managed = Path(folder.path)
        if folder_has_real_files(db, folder.id, managed):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "A linked folder must start empty — create a new folder for the link.",
            )
        unwatch_folder_in_default(folder.id)
        _remove_managed_dir(managed)

    folder.path = str(target)
    folder.source_type = "local_link"

    src = existing or FolderSyncSource(folder_id=folder.id, source_type="local_link")
    src.source_type = "local_link"
    src.ll_path = str(target)
    src.ll_ignore = json.dumps(ignore)
    src.auto_sync_enabled = bool(body.auto_sync_enabled)
    src.auto_sync_hours = int(body.auto_sync_hours)
    src.sync_status = "idle"
    src.sync_error = None
    if existing is None:
        db.add(src)
    db.flush()

    job_id = job_queue.enqueue(
        db, "sync", {"folder_id": folder.id}, dedup_key=f"sync:{folder.id}"
    )
    db.commit()
    db.refresh(src)
    db.refresh(folder)

    # Same live-update contract as the PUT route and gdl_connect: the folder
    # list needs the new path/type, and the modal's per-folder config cache is
    # only as fresh as this event.
    publish_folder_changed(folder, has_sync_source=True)
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
    logger.info("link: folder %d now indexes %s in place", folder.id, target)
    return SyncTriggerOut(folder_id=folder.id, job_id=job_id)
