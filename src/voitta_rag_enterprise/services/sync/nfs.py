"""NFS (local-mount) sync connector.

We treat "NFS" as any local POSIX directory the admin chose to expose
as a sync source. Whether it's a real NFS mount, an SMB mount, a
sshfs, or just a bind-mounted local path is out of scope — the admin
configures the OS-level mount and tells us the resulting path via
the admin settings UI.

Flow
----
* Admin sets ``nfs_root`` in ``settings.json`` (e.g.
  ``/mnt/voitta-nfs``). The setting is empty by default; non-empty
  *and* the path exists turns the feature on system-wide.
* A folder owner configures a sync source of type ``nfs`` and
  picks a subpath relative to ``nfs_root`` via the directory
  picker (server-side, scoped under root).
* On sync the connector walks ``<nfs_root>/<nfs_subpath>``, copies
  every regular file into the folder's filesystem storage, and
  records ``(rel_path, size, mtime_ns)`` in a sidecar for
  change-detection. Files that disappear from the source are
  deleted locally on the next sync.

Same lifecycle as the Drive connector: copy in, watcher picks up
inotify events, indexer extracts. We never index in-place.

Browse endpoint
---------------
``services.sync.nfs.list_children(rel)`` returns the immediate
subdirectory list at ``<nfs_root>/<rel>``, resolved through the
filesystem so symlink escapes are caught. Used by the sync modal's
directory picker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..admin_store import get_nfs_root
from .base import SyncConnector

logger = logging.getLogger(__name__)


# Sidecars written into the folder root so the connector knows what
# came from the NFS source vs what was hand-uploaded. Mirrors the
# Drive connector's ``.voitta_sources.json`` / ``.voitta_timestamps.json``
# pattern; same ignore-glob defaults apply (config.py).
SOURCES_SIDECAR = ".voitta_nfs_sources.json"


@dataclass
class NfsSyncStats:
    files_copied: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path-safety helpers
# ---------------------------------------------------------------------------


def _resolve_under(root: Path, rel: str) -> Path:
    """Return ``root / rel`` resolved on the filesystem AND confirmed to
    still live under ``root``.

    Raises ``ValueError`` for ``..``-escapes, absolute ``rel`` values,
    or symlink redirections that lead outside ``root``. The strict
    resolve uses ``Path.resolve(strict=False)`` then verifies the
    common-ancestor relationship — both ``root`` and the candidate are
    resolved so trailing-slash / case-fold / symlink quirks are
    handled by the OS rather than by string ops.
    """
    root_abs = root.resolve(strict=False)
    raw = (rel or "").strip()
    # Special-case bare "" or "/" → root itself. Anything else with a
    # leading slash is an absolute path the caller shouldn't be asking
    # for; same for Windows-style drive-letter prefixes.
    if raw in ("", "/"):
        return root_abs
    if raw.startswith("/") or raw.startswith("\\") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError("absolute paths are not allowed")
    rel = raw
    # Pre-flight: explicit ``..`` segment rejection. A symlink can still
    # try to escape, but we'll catch that via the post-resolve check.
    parts = [seg for seg in rel.split("/") if seg not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path traversal (``..``) is not allowed")
    candidate = root_abs.joinpath(*parts) if parts else root_abs
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_abs)
    except ValueError as e:  # pragma: no cover — defensive, exercised in tests
        raise ValueError("resolved path escapes the NFS root") from e
    return resolved


def canonicalise_subpaths(subpaths: list[str]) -> list[str]:
    """Normalise a list of POSIX-relative subpaths into a minimal set.

    * Strip leading/trailing slashes; collapse duplicate slashes.
    * Skip blanks.
    * Reject ``..`` segments and absolute paths (delegated to
      :func:`_resolve_under`, but pre-flighted here to keep this pure).
    * Drop duplicates.
    * If ``a/b`` and ``a/b/c`` are both selected, drop ``a/b/c`` (the
      ancestor already covers it). This makes ``NfsConnector.sync``
      idempotent regardless of how messy the UI lets the selection get.
    * Sorted output (deterministic for progress + sidecar diffing).

    The empty string ``""`` (i.e. the whole NFS root) absorbs every
    other entry — if the user explicitly picks the root, that's the
    only canonical subpath.
    """
    cleaned: set[str] = set()
    for raw in subpaths or []:
        if raw is None:
            continue
        s = "/".join(seg for seg in str(raw).split("/") if seg not in ("", "."))
        # Defense against ``..`` / absolute paths — _resolve_under is
        # authoritative; this is just a fast reject.
        if any(part == ".." for part in s.split("/")) or s.startswith("/"):
            continue
        cleaned.add(s)
    if "" in cleaned:
        return [""]
    # Drop entries whose ancestor is also in the set.
    sorted_paths = sorted(cleaned)
    result: list[str] = []
    for path in sorted_paths:
        if any(
            path != ancestor and path.startswith(ancestor + "/")
            for ancestor in result
        ):
            continue
        result.append(path)
    return result


def list_children(rel: str) -> list[dict[str, str]]:
    """Return the immediate-subdirectory listing of ``<root>/<rel>``.

    Used by the sync UI's directory picker. Filters out hidden dirs
    (leading ``.``) and items the admin doesn't have read access to.
    Raises ``ValueError`` if NFS is disabled, ``FileNotFoundError`` if
    the resolved path doesn't exist, ``NotADirectoryError`` if it's
    a file.
    """
    root = get_nfs_root()
    if not root:
        raise ValueError("NFS root is not configured")
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"NFS root does not exist: {root}")
    target = _resolve_under(root_path, rel)
    if not target.exists():
        raise FileNotFoundError(f"path not found: {rel}")
    if not target.is_dir():
        raise NotADirectoryError(f"path is not a directory: {rel}")
    out: list[dict[str, str]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                # Skip files (the picker only walks dirs) and hidden
                # entries — same conventions as Finder / VS Code.
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    # Symlink to a missing target → skip silently.
                    continue
                # Relative path back to the root, for the UI's next
                # browse call. Posix-style separator so the round-trip
                # to the DB stays canonical.
                child_rel = str(target.joinpath(entry.name).resolve().relative_to(root_path.resolve()))
                out.append({"name": entry.name, "rel_path": child_rel})
    except PermissionError as e:
        raise PermissionError(f"cannot list {rel}: {e}") from e
    out.sort(key=lambda x: x["name"].lower())
    return out


# ---------------------------------------------------------------------------
# Sync connector
# ---------------------------------------------------------------------------


class NfsConnector(SyncConnector):
    """Mirror one or more subtrees of the admin's NFS root into the
    folder root.

    Each selected subpath is walked independently. Files land at their
    **full relative path** under the folder root — so picking
    ``data/projectA`` and ``data/projectB`` produces
    ``<folder_root>/data/projectA/...`` and
    ``<folder_root>/data/projectB/...`` (preserving the parent
    structure). This keeps two-subdirectory selections from colliding
    in the local namespace.
    """

    source_type = "nfs"
    supports_progress = True

    def resolve_config(self, row) -> dict:
        import json as _json

        raw = (row.nfs_subpaths or "").strip()
        paths: list[str] = []
        if raw:
            try:
                decoded = _json.loads(raw)
                if isinstance(decoded, list):
                    paths = [str(x) for x in decoded]
            except _json.JSONDecodeError:
                paths = []
        if not paths and row.nfs_subpath:
            paths = [row.nfs_subpath]
        return {"nfs_subpaths": canonicalise_subpaths(paths)}

    async def sync(
        self,
        *,
        folder_root: Path,
        nfs_subpaths: list[str],
        progress_cb: Callable[[str, int, int, dict | None], None] | None = None,
    ) -> NfsSyncStats:
        return await asyncio.to_thread(
            self._sync_sync,
            folder_root=folder_root,
            nfs_subpaths=nfs_subpaths,
            progress_cb=progress_cb,
        )

    def _sync_sync(
        self,
        *,
        folder_root: Path,
        nfs_subpaths: list[str],
        progress_cb: Callable[[str, int, int, dict | None], None] | None,
    ) -> NfsSyncStats:
        def _emit(phase: str, done: int, total: int, detail: dict | None = None) -> None:
            if progress_cb is not None:
                progress_cb(phase, done, total, detail)

        root = get_nfs_root()
        if not root:
            raise RuntimeError("NFS root is not configured")
        root_path = Path(root)
        if not root_path.is_dir():
            raise RuntimeError(f"NFS root does not exist: {root}")
        if not nfs_subpaths:
            raise RuntimeError(
                "NFS: pick at least one folder under the NFS root before syncing."
            )

        # Canonicalise: dedup, drop overlaps so we don't double-walk.
        canonical = canonicalise_subpaths(nfs_subpaths)
        if not canonical:
            raise RuntimeError("NFS: no valid subpaths selected.")
        if len(canonical) > 50:
            logger.warning(
                "NFS sync: %d subpaths selected — large selections may be slow",
                len(canonical),
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        stats = NfsSyncStats()
        _emit("listing", 0, 0)

        # Walk EACH selected subpath, producing ``(src_path, rel_path)``
        # pairs where ``rel_path`` is anchored at ``nfs_root`` (full
        # ``data/projectA/file.txt`` not just ``file.txt``). Sort the
        # listing so the progress fraction is stable across runs.
        entries: list[tuple[Path, str]] = []
        for subpath in canonical:
            source = _resolve_under(root_path, subpath)
            if not source.is_dir():
                stats.errors.append(
                    f"subpath {subpath!r} does not exist or is not a directory"
                )
                continue
            for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                filenames.sort()
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    src = Path(dirpath) / fname
                    if src.is_symlink():
                        # We never follow symlinks during copy — same
                        # posture as the Drive connector, avoids loops
                        # and surprise exfiltration of files outside
                        # the chosen subtree.
                        continue
                    try:
                        if not src.is_file():
                            continue
                    except OSError:
                        continue
                    rel = src.resolve().relative_to(root_path.resolve()).as_posix()
                    entries.append((src, rel))

        total = len(entries)
        _emit("downloading", 0, total)

        # Read previous sidecar (file content fingerprints from the
        # last successful sync). When unchanged-by-size+mtime, skip
        # the copy entirely.
        prev_sidecar = _load_sidecar(folder_root)
        new_sidecar: dict[str, dict[str, int]] = {}

        for idx, (src, rel) in enumerate(entries, start=1):
            try:
                st = src.stat()
                size = int(st.st_size)
                mtime_ns = int(st.st_mtime_ns)
            except OSError as e:
                stats.errors.append(f"stat {rel}: {e}")
                continue
            new_sidecar[rel] = {"size": size, "mtime_ns": mtime_ns}

            dest = folder_root / rel
            prev = prev_sidecar.get(rel)
            if (
                prev is not None
                and dest.exists()
                and prev.get("size") == size
                and prev.get("mtime_ns") == mtime_ns
            ):
                stats.files_unchanged += 1
            else:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # copy2 preserves mtime, which lets the next sync's
                    # unchanged check work without us computing hashes.
                    shutil.copy2(src, dest)
                    stats.files_copied += 1
                    stats.bytes_copied += size
                except OSError as e:
                    stats.errors.append(f"copy {rel}: {e}")
                    continue
            _emit("downloading", idx, total)

        # Remove local files that no longer exist on the source. Skip
        # our own sidecar and anything that wasn't claimed by the
        # *previous* sidecar — hand-uploaded files in the same folder
        # must survive an NFS sync.
        _emit("cleaning", 0, 0)
        expected = set(new_sidecar.keys())
        claimed_before = set(prev_sidecar.keys())
        for rel in claimed_before - expected:
            dest = folder_root / rel
            try:
                if dest.is_file():
                    dest.unlink()
                    stats.files_removed += 1
            except OSError as e:
                stats.errors.append(f"unlink {rel}: {e}")

        # Tidy empty directories left behind by deletes.
        for d in sorted(folder_root.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass

        _save_sidecar(folder_root, new_sidecar)
        _emit("done", total, total)
        return stats


def _load_sidecar(folder_root: Path) -> dict[str, dict[str, int]]:
    p = folder_root / SOURCES_SIDECAR
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def _save_sidecar(folder_root: Path, sidecar: dict[str, dict[str, int]]) -> None:
    p = folder_root / SOURCES_SIDECAR
    p.write_text(json.dumps(sidecar, indent=2, sort_keys=True))


# Public re-exports for the connector registry + REST surface.
__all__ = [
    "NfsConnector",
    "NfsSyncStats",
    "SOURCES_SIDECAR",
    "_resolve_under",
    "canonicalise_subpaths",
    "list_children",
]
