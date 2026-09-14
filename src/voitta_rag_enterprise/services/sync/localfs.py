"""Path-safe helpers for sources rooted at an admin-chosen local directory.

Shared by the NFS connector (which mirrors a subtree into a folder) and the
linked-folder connector (which indexes a subtree in place). Both let a folder
owner browse *below* an admin-set root, so both need the same guarantees: no
``..`` escapes, no absolute paths, no symlink redirections outside the root —
checked on the resolved filesystem path, not by string games.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_under(root: Path, rel: str, *, root_label: str = "root") -> Path:
    """Return ``root / rel`` resolved on the filesystem AND confirmed to still
    live under ``root``.

    Raises ``ValueError`` for ``..``-escapes, absolute ``rel`` values, or
    symlink redirections that lead outside ``root``. Both ``root`` and the
    candidate are resolved so trailing-slash / case-fold / symlink quirks are
    handled by the OS rather than by string ops. ``root_label`` names the root
    in error messages ("NFS root", "linked-folder root").
    """
    root_abs = root.resolve(strict=False)
    raw = (rel or "").strip()
    # Bare "" or "/" → the root itself. Anything else with a leading slash is
    # an absolute path the caller shouldn't be asking for; same for a
    # Windows-style drive-letter prefix.
    if raw in ("", "/"):
        return root_abs
    if raw.startswith("/") or raw.startswith("\\") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError("absolute paths are not allowed")
    # Pre-flight: explicit ``..`` segment rejection. A symlink can still try
    # to escape; the post-resolve check below catches that.
    parts = [seg for seg in raw.split("/") if seg not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path traversal (``..``) is not allowed")
    candidate = root_abs.joinpath(*parts) if parts else root_abs
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_abs)
    except ValueError as e:  # pragma: no cover — defensive, exercised in tests
        raise ValueError(f"resolved path escapes the {root_label}") from e
    return resolved


def list_children(root: Path, rel: str, *, root_label: str = "root") -> list[dict[str, str]]:
    """Return the immediate-subdirectory listing of ``<root>/<rel>``.

    Used by the sync UI's directory pickers. Files and hidden entries
    (leading ``.``) are omitted — the pickers walk directories only. Raises
    ``FileNotFoundError`` if the root or the resolved path doesn't exist,
    ``NotADirectoryError`` if it's a file, ``ValueError`` on an unsafe ``rel``.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"{root_label} does not exist: {root}")
    target = resolve_under(root, rel, root_label=root_label)
    if not target.exists():
        raise FileNotFoundError(f"path not found: {rel}")
    if not target.is_dir():
        raise NotADirectoryError(f"path is not a directory: {rel}")
    root_abs = root.resolve()
    out: list[dict[str, str]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    # Symlink to a missing target → skip silently.
                    continue
                # Relative path back to the root, POSIX separators, for the
                # UI's next browse call.
                child_rel = (
                    target.joinpath(entry.name).resolve().relative_to(root_abs).as_posix()
                )
                out.append({"name": entry.name, "rel_path": child_rel})
    except PermissionError as e:
        raise PermissionError(f"cannot list {rel}: {e}") from e
    out.sort(key=lambda x: x["name"].lower())
    return out
