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
        out[k] = SidecarEntry(
            url=str(url) if isinstance(url, str) else None,
            tab=str(tab) if isinstance(tab, str) else None,
            meta=meta or None,
        )
    return out


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
    if not root.exists() or not root.is_dir():
        logger.warning("folder %s missing or not a directory", folder.path)
        return ScanResult(0, 0, 0)

    # Cloud-local folders (read-only Google Drive mount): two special rules.
    #   1. Liveness guard — if the Drive app is offline or the mount is
    #      transiently empty, a normal scan would see zero files and PURGE the
    #      whole index. Skip the scan entirely instead (never delete on outage).
    #   2. Sidecar lives under data_dir, never inside the Drive mount, so we
    #      never write into the user's Drive.
    # ``scan_roots`` are the subtrees to walk; ``rel`` is always computed
    # relative to ``root`` (folder.path). For a regular folder that's just the
    # folder itself. For a cloud-local (Google Drive) folder, ``root`` is the
    # account mount and we walk ONLY the user-selected subtrees — but still
    # number rel_paths relative to the mount, so the Drive's folder structure
    # (incl. parent dirs the user didn't select) is mirrored under the folder.
    sidecar_file: Path | None = None
    scan_roots: list[Path] = [root]
    if folder.source_type == "google_drive_local":
        import json as _json

        from .sync.cloud_local import cloud_sidecar_path
        from .sync.cloudstorage_local import is_source_live

        if not is_source_live(root):
            logger.info(
                "cloud folder %s not live (Drive offline / empty mount) — "
                "skipping scan to avoid purging the index", folder.path
            )
            return ScanResult(0, 0, 0)
        sidecar_file = cloud_sidecar_path(folder.id)

        # Resolve the selected subtrees from the sync source.
        from ..db.models import FolderSyncSource

        src = session.get(FolderSyncSource, folder.id)
        selected: list[str] = []
        if src is not None:
            raw = (src.gdl_paths or "").strip()
            if raw:
                try:
                    decoded = _json.loads(raw)
                    if isinstance(decoded, list):
                        selected = [str(x) for x in decoded if x]
                except (ValueError, TypeError):
                    selected = []
            if not selected and src.gdl_path:
                selected = [src.gdl_path]
        # Keep only selections that exist and live under the mount; if none are
        # currently present, skip (outage guard — never purge).
        scan_roots = [
            Path(p) for p in selected
            if (Path(p) == root or str(Path(p)).startswith(str(root) + "/"))
            and Path(p).is_dir()
        ]
        if not scan_roots:
            logger.info(
                "cloud folder %s: no selected subtree currently present — "
                "skipping scan to avoid purging the index", folder.path
            )
            return ScanResult(0, 0, 0)

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
        if f.rel_path not in seen:
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
