"""NFS sync source — admin-rooted directory picker + PUT apply logic.

The admin-defined NFS root lives in ``admin_store.settings``; the sync
source records only the user-chosen relative subpaths below it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ....db.models import FolderSyncSource
from ....services.acl import CurrentUser
from ...deps import current_user
from . import registry
from .base import oauth_router

if TYPE_CHECKING:
    from .core import SyncSourceIn


class NfsSyncIn(BaseModel):
    """Payload for ``PUT /folders/{id}/sync`` when ``source_type == 'nfs'``.

    ``subpath`` is kept for backwards-compatibility: old clients post a
    single string; new clients post ``subpaths: list[str]``. The upsert
    handler folds the legacy field into the array if present.
    """

    subpath: str = ""
    subpaths: list[str] = Field(default_factory=list)


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


def status_snapshot() -> tuple[str, bool, str]:
    """Return ``(nfs_root, available, status)`` for inclusion in NFS
    sync-source responses. Re-checks on every call — an unmounted NFS
    flips the feature off without a restart."""
    from ....services.admin_store import get_nfs_root

    root = get_nfs_root()
    if not root:
        return "", False, "disabled"
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


def decode_subpaths(src: FolderSyncSource) -> list[str]:
    """Read the NFS row's selected subpaths.

    Prefer the new ``nfs_subpaths`` JSON column; fall back to the
    legacy single-string ``nfs_subpath`` so rows saved before the
    multi-select migration still render correctly. Always returns a
    canonical (deduped, no-overlap) list.
    """
    from ....services.sync.nfs import canonicalise_subpaths

    raw = (src.nfs_subpaths or "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return canonicalise_subpaths([str(x) for x in decoded])
        except json.JSONDecodeError:
            pass
    legacy = (src.nfs_subpath or "").strip()
    if legacy:
        return canonicalise_subpaths([legacy])
    return []


def clear_fields(src: FolderSyncSource) -> None:
    src.nfs_subpath = None
    src.nfs_subpaths = None


def build_out(src: FolderSyncSource) -> NfsSyncOut:
    root, available, status_str = status_snapshot()
    subpaths = decode_subpaths(src)
    return NfsSyncOut(
        # Legacy single-value echo: first element, or "" when the
        # root itself is the only selection. Old clients can keep
        # reading ``subpath`` until they migrate.
        subpath=subpaths[0] if subpaths else "",
        subpaths=subpaths,
        nfs_root=root,
        nfs_available=available,
        nfs_status=status_str,
    )


def apply_config(
    *,
    body: SyncSourceIn,
    existing: FolderSyncSource | None,
    folder_id: int,
) -> FolderSyncSource:
    cfg = body.nfs
    if cfg is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Missing 'nfs' config for source_type='nfs'",
        )
    # NFS must be enabled + the configured root must be reachable.
    # Re-check at every save so an admin who removes the NFS root
    # immediately blocks new sync sources.
    root, available, status_str = status_snapshot()
    if not available:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"NFS is not available ({status_str}); ask an admin to "
            "configure the NFS root.",
        )
    # Validate every chosen subpath: must resolve under the root,
    # no ``..``, no symlink escapes. Backwards-compat: if the client
    # only sent ``subpath`` (old single-value field), fold it into
    # the array.
    from ....services.sync.nfs import _resolve_under, canonicalise_subpaths

    raw_paths: list[str] = list(cfg.subpaths or [])
    if not raw_paths and (cfg.subpath or "").strip():
        raw_paths = [cfg.subpath]
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

    src = existing or FolderSyncSource(folder_id=folder_id, source_type="nfs")
    if existing is not None and existing.source_type != "nfs":
        registry.clear_other_sources(src, "nfs")
    src.source_type = "nfs"
    src.nfs_subpaths = json.dumps(subpaths)
    # Keep the legacy column populated with the first entry so old
    # clients reading the unmigrated DB still see *something*.
    src.nfs_subpath = subpaths[0] if subpaths else None
    return src


def trigger_check(src: FolderSyncSource) -> None:
    _root, available, status_str = status_snapshot()
    if not available:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"NFS is not available ({status_str})",
        )
    if not decode_subpaths(src):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick at least one folder under the NFS root before syncing.",
        )


registry.register(
    registry.SourceHandler(
        source_type="nfs",
        out_field="nfs",
        apply=apply_config,
        build_out=build_out,
        clear=clear_fields,
        trigger_check=trigger_check,
    )
)


# ---------------------------------------------------------------------------
# Capability check + directory picker (folder-agnostic).
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
    root, available, status_str = status_snapshot()
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
    from ....services.sync.nfs import list_children

    _root, available, status_str = status_snapshot()
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
