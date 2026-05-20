"""Shared helpers for Microsoft exporters.

* :class:`RemoteEntry` — same idea as the Google equivalent: one local
  file the connector will write. Carries the deep-link URL for the
  ``.voitta_sources.json`` sidecar.
* ``fingerprint_*`` — leading ``<!--voitta-fingerprint:HASH-->`` header
  on markdown outputs so the next sync can skip unchanged renders.
* ``atomic_write_*`` — write-to-temp + rename, matching the gdrive
  atomic helpers so partial writes never appear on disk.
"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"

_SAFE_NAME_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def safe_filename(name: str, fallback: str = "item") -> str:
    """Sanitize a remote name for use as a local path component."""
    out = _SAFE_NAME_RE.sub("-", name or "").strip().strip(".")
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"-{2,}", "-", out)
    out = out.strip("-")
    return out[:80] or fallback


@dataclass
class RemoteEntry:
    """One file the connector should materialise.

    ``rel_path`` is relative to the local folder root. ``url`` is the
    deep link recorded in ``.voitta_sources.json``. ``fingerprint``
    short-circuits unchanged re-renders. ``payload`` is set for
    rendered text outputs; binary download producers write bytes
    directly to ``rel_path`` and set ``payload=None``.
    """

    rel_path: str
    url: str
    fingerprint: str
    payload: str | None = None
    mtime: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def fingerprint_header(fingerprint: str) -> str:
    return f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n"


def fingerprint_matches(local_path: Path, fingerprint: str) -> bool:
    """True iff ``local_path`` already starts with this fingerprint header."""
    if not local_path.exists() or not fingerprint:
        return False
    try:
        # Cheap — read just the first line; fingerprint headers are short.
        with local_path.open("rb") as f:
            head = f.readline(2048)
    except OSError:
        return False
    expected = fingerprint_header(fingerprint).encode("utf-8").rstrip(b"\n")
    return head.rstrip(b"\n") == expected


def atomic_write_text(path: Path, content: str, mtime: float | None = None) -> None:
    """Write ``content`` to ``path`` via a temp file + rename.

    ``mtime`` lets the connector preserve the remote modified time on
    the local file — important for Teams meetings (where the user
    asked us to keep timestamps) and useful elsewhere for cheap
    "changed since" checks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    if mtime is not None:
        with suppress(OSError):
            os.utime(path, (mtime, mtime))


def atomic_write_bytes(path: Path, data: bytes, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    if mtime is not None:
        with suppress(OSError):
            os.utime(path, (mtime, mtime))


