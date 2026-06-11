"""Local cloud-storage (Google Drive for Desktop) inspection helpers.

The desktop app rides on top of the *already-installed* Google Drive for
Desktop application. macOS exposes each signed-in account as a File Provider
mount under ``~/Library/CloudStorage/GoogleDrive-<email>/``. Files there are
**dataless stubs** (metadata present, ``st_blocks == 0``); *reading* a stub
makes the Drive app download the whole file on demand. This module is the
pure-stdlib, read-only toolkit the connector + routes build on:

* :func:`list_accounts`  — enumerate signed-in Google Drive accounts.
* :func:`browse`         — list a directory's children (free; never downloads).
* :func:`is_stub`        — dataless-stub detection by ``stat`` alone.
* :func:`is_drive_app_running` — liveness guard (reads only work when it runs).
* :func:`read_gdoc_pointer` — parse a ``.gdoc/.gsheet/.gslides`` pointer file.
* :func:`assert_read_only_path` — refuse any *write* under CloudStorage.

**Hard rule:** nothing here (or in any caller) ever writes inside
``~/Library/CloudStorage``. We read; we never push back. :func:`is_within_cloud_storage`
and :func:`assert_read_only_path` exist so that rule can be enforced in code,
not just by convention.

macOS-only. On other platforms the enumeration simply yields nothing.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Root of the per-provider File Provider mounts on macOS.
CLOUD_STORAGE_ROOT = Path.home() / "Library" / "CloudStorage"

# Google Drive mounts are dirs named ``GoogleDrive-<account-email>``.
_GDRIVE_PREFIX = "GoogleDrive-"

# Native Google Workspace docs are tiny JSON pointer files, not real content.
GOOGLE_DOC_SUFFIXES = (".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gtable")

# Map a native-doc suffix → (Workspace product path, default export format).
# Used by the export job; kept here so the suffix knowledge lives in one place.
GOOGLE_DOC_EXPORT = {
    ".gdoc": ("document", "pdf"),
    ".gsheet": ("spreadsheets", "xlsx"),
    ".gslides": ("presentation", "pdf"),
    ".gdraw": ("drawings", "pdf"),
}


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_dataless_stub(st: os.stat_result) -> bool:
    """True if a stat result describes a cloud placeholder with no local
    bytes (a File Provider "dataless" file): nonzero logical size but zero
    allocated blocks. *Reading* such a file forces the cloud provider to
    download the full content synchronously — callers that only want local
    bytes must check this first."""
    return st.st_size > 0 and getattr(st, "st_blocks", 0) == 0


# ---------------------------------------------------------------------------
# Write-safety — the load-bearing guard for "never touch the Drive"
# ---------------------------------------------------------------------------


def is_within_cloud_storage(path: os.PathLike | str) -> bool:
    """True if ``path`` resolves to somewhere under ``~/Library/CloudStorage``.

    Used to (a) validate that a picked source really is a cloud mount and
    (b) refuse any write whose target lands inside the mount.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        root = CLOUD_STORAGE_ROOT.resolve(strict=False)
    except OSError:
        return False
    if resolved == root:
        return True
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def assert_read_only_path(path: os.PathLike | str) -> None:
    """Raise if ``path`` is under CloudStorage — i.e. refuse to *write* there.

    Call this immediately before any filesystem write the connector performs,
    so a bug can never push data back into the user's Drive. Reads are always
    fine; this guards writes only.
    """
    if is_within_cloud_storage(path):
        raise PermissionError(
            f"refusing to write inside the cloud-storage mount (read-only): {path}"
        )


# ---------------------------------------------------------------------------
# Account enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudAccount:
    """A signed-in Google Drive for Desktop account."""

    email: str          # parsed from the mount dir name
    path: str           # absolute mount root, e.g. .../GoogleDrive-foo@bar.com
    provider: str = "google_drive"

    def as_dict(self) -> dict:
        return {"email": self.email, "path": self.path, "provider": self.provider}


def list_accounts() -> list[CloudAccount]:
    """Enumerate Google Drive for Desktop accounts mounted on this machine.

    Returns ``[]`` off macOS, or when the Drive app has never run / signed in.
    Cheap: a single directory listing, no file content touched.
    """
    if not is_macos() or not CLOUD_STORAGE_ROOT.is_dir():
        return []
    accounts: list[CloudAccount] = []
    try:
        entries = sorted(CLOUD_STORAGE_ROOT.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if not name.startswith(_GDRIVE_PREFIX):
            continue
        if not entry.is_dir():
            continue
        email = name[len(_GDRIVE_PREFIX):] or "(unknown)"
        accounts.append(CloudAccount(email=email, path=str(entry)))
    return accounts


# ---------------------------------------------------------------------------
# Liveness — reads only materialize while the Drive app is running
# ---------------------------------------------------------------------------


def is_drive_app_running() -> bool:
    """True if Google Drive for Desktop appears to be running.

    Reading a stub only materializes content while the app is alive to serve
    the download; otherwise reads return empty/garbage. We check the process
    table (no extra deps) for the known process name.
    """
    if not is_macos():
        return False
    try:
        import subprocess

        out = subprocess.run(
            ["pgrep", "-x", "Google Drive"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return True
        # Fallback: broader match (the helper/agent processes).
        out = subprocess.run(
            ["pgrep", "-f", "Google Drive"],
            capture_output=True, text=True, timeout=5,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def is_source_live(path: os.PathLike | str) -> bool:
    """True if the cloud source at ``path`` is safe to scan/index right now:
    the mount exists, is non-empty, and the Drive app is running.

    The non-empty check is the key safeguard against the index-purge bug — a
    transiently empty mount (app starting, signed out) must NOT be mistaken for
    "all files deleted". Callers use this to *skip* a scan rather than purge.
    """
    p = Path(path)
    if not p.is_dir():
        return False
    if not is_drive_app_running():
        return False
    try:
        next(p.iterdir())  # at least one entry
    except StopIteration:
        return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Stub detection + browsing (read-only, never downloads)
# ---------------------------------------------------------------------------


def is_stub(path: os.PathLike | str) -> bool:
    """True if ``path`` is a dataless cloud stub (content not on disk yet).

    Pure ``stat`` — no read, no download. A stub has a logical size but zero
    allocated blocks. Materialized files (and tiny files that fit in inline
    extents) report non-zero blocks.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_size > 0 and st.st_blocks == 0


def is_native_doc(path: os.PathLike | str) -> bool:
    """True for Google Workspace pointer files (.gdoc/.gsheet/.gslides/…)."""
    return Path(path).suffix.lower() in GOOGLE_DOC_SUFFIXES


@dataclass(frozen=True)
class BrowseEntry:
    name: str
    path: str
    is_dir: bool
    is_native_doc: bool
    is_stub: bool
    size: int  # logical size (bytes); 0 for dirs

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "is_native_doc": self.is_native_doc,
            "is_stub": self.is_stub,
            "size": self.size,
        }


def browse(path: os.PathLike | str) -> list[BrowseEntry]:
    """List the immediate children of a directory under CloudStorage.

    Free and read-only: uses ``os.scandir`` + ``stat`` only; never opens file
    content, so nothing is downloaded. Raises ``ValueError`` if ``path`` is not
    within ``~/Library/CloudStorage`` (path-safety: callers pass user input).
    Directories sort first, then files, both case-insensitively.
    """
    target = Path(path).expanduser()
    if not is_within_cloud_storage(target):
        raise ValueError("path is not within ~/Library/CloudStorage")
    resolved = target.resolve(strict=False)
    if not is_within_cloud_storage(resolved):  # post-resolve symlink-escape check
        raise ValueError("resolved path escapes ~/Library/CloudStorage")
    if not resolved.is_dir():
        raise ValueError(f"not a directory: {resolved}")

    out: list[BrowseEntry] = []
    with os.scandir(resolved) as it:
        for de in it:
            name = de.name
            if name.startswith("."):
                continue  # skip dotfiles / .DS_Store / our sidecars if any
            try:
                is_dir = de.is_dir(follow_symlinks=False)
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            out.append(
                BrowseEntry(
                    name=name,
                    path=de.path,
                    is_dir=is_dir,
                    is_native_doc=(not is_dir and is_native_doc(name)),
                    is_stub=(not is_dir and st.st_size > 0 and st.st_blocks == 0),
                    size=(0 if is_dir else st.st_size),
                )
            )
    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


# ---------------------------------------------------------------------------
# Native-doc pointer parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GDocPointer:
    doc_id: str
    email: str | None
    url: str | None
    kind: str   # Workspace product path: document / spreadsheets / presentation
    export_format: str


def read_gdoc_pointer(path: os.PathLike | str) -> GDocPointer | None:
    """Parse a ``.gdoc/.gsheet/.gslides`` pointer file.

    These are tiny JSON files: ``{"doc_id": "...", "email": "...", "url": ...}``.
    The actual document content lives only in the cloud, so the connector
    records the URL (link-only) and an export job may later fetch a rendered
    copy. Returns ``None`` if the file isn't a recognised, parseable pointer.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in GOOGLE_DOC_EXPORT:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    doc_id = data.get("doc_id") or data.get("resource_id") or ""
    if not doc_id:
        return None
    kind, fmt = GOOGLE_DOC_EXPORT[suffix]
    url = data.get("url")
    if not url and doc_id:
        url = f"https://docs.google.com/{kind}/d/{doc_id}"
    return GDocPointer(
        doc_id=str(doc_id),
        email=data.get("email"),
        url=url,
        kind=kind,
        export_format=fmt,
    )
