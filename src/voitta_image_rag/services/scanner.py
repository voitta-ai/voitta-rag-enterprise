"""Folder scanner — reconciles SQLite ``files`` rows against the live filesystem."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
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


@dataclass
class SidecarEntry:
    """Per-file metadata read from ``.voitta_sources.json``."""

    url: str | None = None
    tab: str | None = None


def load_sidecar(folder_root: Path) -> dict[str, SidecarEntry]:
    """Return the ``rel_path → SidecarEntry`` mapping from ``.voitta_sources.json``.

    Two on-disk shapes are accepted so older sidecars keep working:

    - ``{rel_path: "https://…"}`` — URL only (legacy)
    - ``{rel_path: {"url": "https://…", "tab": "Overview"}}`` — extended
    """
    sidecar = folder_root / SIDECAR_FILENAME
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
        if not isinstance(k, str):
            continue
        if isinstance(v, str):
            out[k] = SidecarEntry(url=v)
        elif isinstance(v, dict):
            url = v.get("url")
            tab = v.get("tab")
            out[k] = SidecarEntry(
                url=str(url) if isinstance(url, str) else None,
                tab=str(tab) if isinstance(tab, str) else None,
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
        from ..config import get_settings

        max_file_bytes = get_settings().max_file_bytes

    root = Path(folder.path)
    if not root.exists() or not root.is_dir():
        logger.warning("folder %s missing or not a directory", folder.path)
        return ScanResult(0, 0, 0)

    sidecar = load_sidecar(root)
    now = int(time.time())
    seen: set[str] = set()
    added = updated = 0

    for path in root.rglob("*"):
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
            )
            session.add(new_file)
            session.flush()
            job_queue.enqueue(
                session, "extract", {"file_id": new_file.id}, dedup_key=f"extract:{new_file.id}"
            )
            added += 1
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
            if existing.state == "deleted":
                existing.state = "pending"
            if changed:
                job_queue.enqueue(
                    session,
                    "extract",
                    {"file_id": existing.id},
                    dedup_key=f"extract:{existing.id}",
                )
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

    return ScanResult(added=added, updated=updated, vanished=vanished)
