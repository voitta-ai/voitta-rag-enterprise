"""Google Drive local-sync connector (desktop, no credentials).

Rides on the already-installed *Google Drive for Desktop* app. Unlike the
OAuth ``google_drive`` connector (which downloads via the API into a managed
folder), this connector **indexes the Drive mount in place**: the folder's
``path`` *is* the chosen subtree under ``~/Library/CloudStorage/GoogleDrive-…``.

Consequences that shape this module:

* **We never write into the Drive.** ``sync`` performs no download and no
  in-tree write; ``scanner`` only ``stat``s, so scanning costs nothing. The
  extract worker indexes files that have local bytes; *dataless* placeholders
  are parked as unsupported ("cloud-only") rather than read, because a read
  makes the Drive app download the full content synchronously — set
  ``VOITTA_CLOUD_MATERIALIZE_ON_INDEX=true`` to restore download-on-read.
  The provenance **sidecar lives under ``data_dir``
  (:func:`cloud_sidecar_path`), never inside the mount.** Every write target
  is asserted read-only-safe via
  :func:`cloudstorage_local.assert_read_only_path`.
* **Read-only / outage-safe.** ``sync`` refuses to run unless the source is
  live (Drive app running, mount non-empty); ``scanner`` independently skips a
  scan when the source isn't live, so a transient outage never purges the index.
* **Native Google docs** (``.gdoc`` etc.) have no local content; the connector
  records their web URL in the sidecar so they're searchable as links, and the
  ``export_gdoc`` job opportunistically fetches a rendered copy of *shared* ones.

macOS + desktop (single-user) only; gated by callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...config import get_settings
from .base import SyncConnector
from .cloudstorage_local import (
    GOOGLE_DOC_SUFFIXES,
    assert_read_only_path,
    is_dataless_stub,
    is_native_doc,
    is_source_live,
    is_within_cloud_storage,
    read_gdoc_pointer,
)

logger = logging.getLogger(__name__)

SOURCE_TYPE = "google_drive_local"


def cloud_sidecar_path(folder_id: int) -> Path:
    """Where this folder's provenance sidecar lives — under ``data_dir``, NOT
    inside the Drive mount. Keeping it here is what guarantees we never write
    into the user's Google Drive."""
    return get_settings().data_dir / "cloud_sidecars" / f"{folder_id}.json"


@dataclass
class CloudLocalSyncStats:
    files_seen: int = 0
    native_docs: int = 0
    stubs: int = 0
    materialized: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_seen": self.files_seen,
            "native_docs": self.native_docs,
            "stubs": self.stubs,
            "materialized": self.materialized,
            "errors": self.errors,
        }


class CloudLocalConnector(SyncConnector):
    source_type = SOURCE_TYPE
    supports_progress = True

    def resolve_config(self, row) -> dict[str, Any]:  # FolderSyncSource
        import json as _json

        paths: list[str] = []
        raw = (row.gdl_paths or "").strip()
        if raw:
            try:
                decoded = _json.loads(raw)
                if isinstance(decoded, list):
                    paths = [str(x) for x in decoded if x]
            except (ValueError, TypeError):
                paths = []
        if not paths and row.gdl_path:
            paths = [row.gdl_path]
        return {
            "gdl_paths": paths,
            "gdl_account": (row.gdl_account or "").strip(),
            "folder_id": row.folder_id,
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        gdl_paths: list[str] | None = None,
        gdl_account: str = "",
        folder_id: int | None = None,
        progress_cb: Callable[[str, int, int, dict | None], None] | None = None,
    ) -> "CloudLocalSyncStats":
        # run_sync expects a stats OBJECT (it calls .as_dict() / .errors on the
        # result), so return the dataclass — not its dict form.
        return await asyncio.to_thread(
            self._sync_sync,
            folder_root=folder_root,
            gdl_paths=gdl_paths or [],
            gdl_account=gdl_account,
            folder_id=folder_id,
            progress_cb=progress_cb,
        )

    def _sync_sync(
        self,
        *,
        folder_root: Path,
        gdl_paths: list[str],
        gdl_account: str,
        folder_id: int | None,
        progress_cb: Callable[[str, int, int, dict | None], None] | None,
    ) -> "CloudLocalSyncStats":
        def _emit(phase: str, done: int, total: int, detail: dict | None = None) -> None:
            if progress_cb is not None:
                progress_cb(phase, done, total, detail)

        # ``folder_root`` is the account MOUNT root; rel_paths are numbered
        # relative to it so the Drive structure is mirrored under the folder.
        mount = Path(folder_root).expanduser()

        # --- safety + liveness gates ---------------------------------------
        if not is_within_cloud_storage(mount):
            raise RuntimeError(
                f"cloud-local source is not under ~/Library/CloudStorage: {mount}"
            )
        if not is_source_live(mount):
            raise RuntimeError(
                "Google Drive is not available (app not running). "
                "Start Google Drive for Desktop and try again."
            )
        # Only the selected subtrees that currently exist under the mount.
        subtrees = [
            Path(p) for p in (gdl_paths or [])
            if (Path(p) == mount or str(Path(p)).startswith(str(mount) + "/"))
            and Path(p).is_dir()
        ]
        if not subtrees:
            raise RuntimeError("no selected Google Drive folder is currently present")

        stats = CloudLocalSyncStats()
        _emit("listing", 0, 0)

        # Walk the SELECTED subtrees (stat-only — NO content read, NO download)
        # to build the provenance sidecar. Real files need no sidecar entry
        # (scanner handles them); native docs get their web URL recorded so
        # they're searchable as links and the export job can fetch shared ones.
        # Sidecar keys are rel-to-MOUNT, matching what the scanner records.
        sidecar: dict[str, dict] = {}
        files: list[Path] = []
        for sroot in subtrees:
            for dirpath, dirnames, filenames in os.walk(sroot, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fname in sorted(filenames):
                    if fname.startswith("."):
                        continue
                    files.append(Path(dirpath) / fname)

        total = len(files)
        for i, fpath in enumerate(files):
            try:
                rel = fpath.relative_to(mount).as_posix()
            except ValueError:
                continue
            stats.files_seen += 1
            try:
                st = fpath.stat()  # metadata only — does not materialize
                if is_dataless_stub(st):
                    stats.stubs += 1
                else:
                    stats.materialized += 1
            except OSError:
                pass

            if is_native_doc(fpath):
                stats.native_docs += 1
                ptr = read_gdoc_pointer(fpath)
                if ptr is not None:
                    entry: dict[str, Any] = {}
                    if ptr.url:
                        entry["url"] = ptr.url
                    # Stash export hints for the export_gdoc job (ignored by the
                    # scanner's sidecar reader, which only knows url/tab/meta).
                    entry["gdoc"] = {
                        "doc_id": ptr.doc_id,
                        "kind": ptr.kind,
                        "format": ptr.export_format,
                    }
                    sidecar[rel] = entry

            if i % 200 == 0:
                _emit("listing", i, total)

        # --- persist the sidecar UNDER data_dir (never inside the Drive) ----
        fid = folder_id if folder_id is not None else 0
        sidecar_path = cloud_sidecar_path(fid)
        assert_read_only_path(sidecar_path)  # belt-and-suspenders: must be outside the mount
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True))

        _emit("done", total, total)
        logger.info(
            "cloud-local sync: %d files (%d stubs, %d native docs) across %d subtree(s) under %s",
            stats.files_seen, stats.stubs, stats.native_docs, len(subtrees), mount,
        )
        return stats  # run_sync calls .as_dict()/.errors on the OBJECT


__all__ = [
    "CloudLocalConnector",
    "CloudLocalSyncStats",
    "SOURCE_TYPE",
    "cloud_sidecar_path",
    "GOOGLE_DOC_SUFFIXES",
]
