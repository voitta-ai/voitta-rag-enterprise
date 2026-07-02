"""Folder scanner — reconciles SQLite ``files`` rows against the live filesystem."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import File, Folder
from . import job_queue
from .ignore import IgnoreMatcher
from .ignore import from_settings as _ignore_from_settings

logger = logging.getLogger(__name__)

SIDECAR_FILENAME = ".voitta_sources.json"


@dataclass
class ScanResult:
    added: int
    updated: int
    vanished: int
    # File ids that were newly inserted, mtime-bumped, or state-flipped.
    # Caller is expected to publish ``file.upserted`` for each (and
    # ``file.deleted`` for ``vanished_ids``) after committing the
    # session, so the SPA's files store reflects the scan in real time.
    # Without this the SPA only sees scan-driven inserts on its next
    # full ``listAllFiles`` round-trip — which only happens on the next
    # WS connect.
    touched_ids: list[int] = field(default_factory=list)
    vanished_ids: list[int] = field(default_factory=list)


# Source-provenance keys a connector may write into a sidecar record alongside
# url/tab (see services/source_meta.build). Stored verbatim on File.source_meta.
_META_KEYS = (
    "owner_name", "owner_email", "editor_name", "editor_email",
    "shared_by_name", "shared_by_email", "created_ts", "modified_ts",
)
# Nested attribute dicts a rich connector (Jira/Confluence) may also write:
# ``attrs`` (curated, filterable) and ``attrs_raw`` (full bag). Carried through
# verbatim so source_meta.payload_fields can expand them into attr_* fields.
_META_DICT_KEYS = ("attrs", "attrs_raw")


@dataclass
class SidecarEntry:
    """Per-file metadata read from ``.voitta_sources.json``."""

    url: str | None = None
    tab: str | None = None
    # Source-object provenance (owner/editor/shared_by + created/modified
    # epochs); the subset of keys the connector recorded. None when absent.
    meta: dict | None = None


def load_sidecar(folder_root: Path, sidecar_file: Path | None = None) -> dict[str, SidecarEntry]:
    """Return the ``rel_path → SidecarEntry`` mapping from a sidecar JSON file.

    On-disk shape: ``{rel_path: {"url": …, "tab": …, "owner_email": …,
    "created_ts": …, …}}``. Unknown keys are ignored (forward-compat).

    By default the sidecar is ``folder_root/.voitta_sources.json``. Cloud-local
    folders (whose ``folder_root`` is the user's read-only Drive mount) pass an
    explicit ``sidecar_file`` under ``data_dir`` so we never write into the Drive.
    """
    sidecar = sidecar_file if sidecar_file is not None else folder_root / SIDECAR_FILENAME
    if not sidecar.exists():
        return {}
    try:
        data = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("sidecar parse failed at %s: %s", sidecar, e)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, SidecarEntry] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        url = v.get("url")
        tab = v.get("tab")
        meta = {mk: v[mk] for mk in _META_KEYS if mk in v and v[mk] is not None}
        for dk in _META_DICT_KEYS:
            if isinstance(v.get(dk), dict) and v[dk]:
                meta[dk] = v[dk]
        out[k] = SidecarEntry(
            url=str(url) if isinstance(url, str) else None,
            tab=str(tab) if isinstance(tab, str) else None,
            meta=meta or None,
        )
    return out


def resolve_scan_roots(session: Session, folder: Folder) -> list[Path] | None:
    """The subtrees a scan of ``folder`` walks, or ``None`` when the folder's
    disk state cannot be trusted right now.

    ``None`` is a hard "hands off" answer — root missing, cloud mount offline
    or transiently empty, no selected subtree present. Callers (the scan, the
    startup-recovery sweep) must then neither purge nor repair anything for
    this folder: absence of evidence is not evidence of absence.

    For a regular folder this is just ``[folder.path]``. For a cloud-local
    (Google Drive mount) folder, ``folder.path`` is the account mount and only
    the user-selected subtrees are in scope — a file under a *deselected*
    subtree exists on disk but does not belong in the index. rel_paths are
    always relative to ``folder.path`` regardless, so the Drive's folder
    structure is mirrored under the folder.
    """
    root = Path(folder.path)
    if not root.is_dir():
        logger.warning("folder %s missing or not a directory", folder.path)
        return None
    if folder.source_type != "google_drive_local":
        return [root]

    from .sync.cloudstorage_local import is_source_live

    # Liveness guard — an offline Drive app or a transiently empty mount
    # must never be mistaken for "all files deleted".
    if not is_source_live(root):
        logger.info(
            "cloud folder %s not live (Drive offline / empty mount) — "
            "disk state unknown", folder.path
        )
        return None

    from ..db.models import FolderSyncSource

    src = session.get(FolderSyncSource, folder.id)
    selected: list[str] = []
    if src is not None:
        raw = (src.gdl_paths or "").strip()
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, list):
                    selected = [str(x) for x in decoded if x]
            except (ValueError, TypeError):
                selected = []
        if not selected and src.gdl_path:
            selected = [src.gdl_path]
    # Keep only selections that exist and live under the mount.
    roots = [
        Path(p) for p in selected
        if (Path(p) == root or str(Path(p)).startswith(str(root) + "/"))
        and Path(p).is_dir()
    ]
    if not roots:
        logger.info(
            "cloud folder %s: no selected subtree currently present — "
            "disk state unknown", folder.path
        )
        return None
    return roots


def file_present_in_scope(
    root: Path, scan_roots: list[Path], ignore: IgnoreMatcher, rel_path: str
) -> bool:
    """True when ``rel_path`` exists as a regular file under one of the
    scanned subtrees and isn't ignore-listed — i.e. a scan walking right now
    would (re)index it.

    The single answer to "is this file really here?", shared by the scan's
    vanish sweep and the startup-recovery sweep so the two can never disagree.
    Unreadable (OSError on stat) counts as absent: a file the indexer cannot
    stat cannot be indexed either.
    """
    if ignore.matches(rel_path):
        return False
    p = root / rel_path
    if not any(p == r or str(p).startswith(f"{r}/") for r in scan_roots):
        return False
    try:
        return p.is_file()
    except OSError:
        return False


def scan_folder(
    session: Session,
    folder: Folder,
    ignore: IgnoreMatcher | None = None,
    max_file_bytes: int | None = None,
) -> ScanResult:
    """Walk ``folder.path``, upsert ``files`` rows, mark vanished files deleted.

    Caller is responsible for committing the session.
    """
    if ignore is None:
        ignore = _ignore_from_settings()
    if max_file_bytes is None:
        from .indexing_caps import get_caps

        # Admin-tunable override; falls back to the Settings default
        # baked into the IndexingCaps dataclass when no override exists.
        max_file_bytes = get_caps().max_file_bytes

    root = Path(folder.path)
    scan_roots = resolve_scan_roots(session, folder)
    if scan_roots is None:
        return ScanResult(0, 0, 0)

    # Cloud-local sidecar lives under data_dir, never inside the (read-only)
    # Drive mount; regular folders keep it at the folder root.
    sidecar_file: Path | None = None
    if folder.source_type == "google_drive_local":
        from .sync.cloud_local import cloud_sidecar_path

        sidecar_file = cloud_sidecar_path(folder.id)

    sidecar = load_sidecar(root, sidecar_file)
    now = int(time.time())
    seen: set[str] = set()
    added = updated = 0
    touched_ids: list[int] = []
    vanished_ids: list[int] = []

    _walk_iter = (
        p for sroot in scan_roots for p in sroot.rglob("*")
    )
    for path in _walk_iter:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.name == SIDECAR_FILENAME or ignore.matches(rel):
            continue
        try:
            stat = path.stat()
        except OSError as e:
            logger.warning("stat failed for %s: %s", path, e)
            continue
        if stat.st_size > max_file_bytes:
            logger.info("skip oversize %s (%d > %d)", rel, stat.st_size, max_file_bytes)
            continue

        seen.add(rel)
        entry = sidecar.get(rel)
        url = entry.url if entry else None
        tab = entry.tab if entry else None
        meta_json = (
            json.dumps(entry.meta) if (entry and entry.meta) else None
        )

        existing = session.execute(
            select(File).where(File.folder_id == folder.id, File.rel_path == rel)
        ).scalar_one_or_none()

        if existing is None:
            new_file = File(
                folder_id=folder.id,
                rel_path=rel,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                last_seen_at=now,
                state="pending",
                source_url=url,
                tab=tab,
                source_meta=meta_json,
            )
            session.add(new_file)
            session.flush()
            job_queue.enqueue(
                session, "extract", {"file_id": new_file.id}, dedup_key=f"extract:{new_file.id}"
            )
            added += 1
            touched_ids.append(new_file.id)
        else:
            changed = (
                existing.mtime_ns != stat.st_mtime_ns
                or existing.size_bytes != stat.st_size
                or existing.state == "deleted"
            )
            existing.last_seen_at = now
            existing.size_bytes = stat.st_size
            existing.mtime_ns = stat.st_mtime_ns
            if existing.source_url != url:
                existing.source_url = url
            if existing.tab != tab:
                existing.tab = tab
            if existing.source_meta != meta_json:
                existing.source_meta = meta_json
            if existing.state == "deleted":
                existing.state = "pending"
            if changed:
                job_queue.enqueue(
                    session,
                    "extract",
                    {"file_id": existing.id},
                    dedup_key=f"extract:{existing.id}",
                )
                touched_ids.append(existing.id)
            updated += 1

    vanished = 0
    rows = (
        session.execute(
            select(File).where(File.folder_id == folder.id, File.state != "deleted")
        )
        .scalars()
        .all()
    )
    for f in rows:
        if f.rel_path in seen:
            continue
        # Not seen by the walk — but that alone isn't proof it's gone: a file
        # can land while the walk is running (upload burst, git sync), with
        # its row inserted by the watcher after the walk already passed its
        # directory. Purging those made freshly-uploaded files disappear from
        # the index while sitting on disk. Re-stat before declaring vanished.
        if file_present_in_scope(root, scan_roots, ignore, f.rel_path):
            continue
        f.state = "deleted"
        job_queue.enqueue(
            session, "delete_file", {"file_id": f.id}, dedup_key=f"delete:{f.id}"
        )
        vanished += 1
        vanished_ids.append(f.id)

    return ScanResult(
        added=added,
        updated=updated,
        vanished=vanished,
        touched_ids=touched_ids,
        vanished_ids=vanished_ids,
    )
