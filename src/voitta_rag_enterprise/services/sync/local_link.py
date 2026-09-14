"""Linked-folder connector — a host directory indexed IN PLACE.

The folder's ``path`` IS the linked directory (set by the connect endpoint in
``api/routes/sync/local_link.py``). There is nothing to mirror: ``sync`` only
confirms the directory is still there, and the post-sync rescan that
``run_sync`` performs for every connector does the actual work — it walks the
tree where it is, applying the folder's own ignore list on top of the global
one, and enqueues extracts for new or changed files. Nothing is copied and
nothing is ever written into the linked tree (the scanner keeps this folder's
sidecar under ``data_dir``, see ``services.in_place``).

Refresh cadence is the sync row's ``auto_sync_hours`` — the same scheduler
every other connector uses. There is deliberately no filesystem watcher on a
linked tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ignore import parse_patterns
from .base import SyncConnector


@dataclass
class LocalLinkSyncStats:
    path: str
    ignore: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "indexed_in_place": True,
            "ignore": list(self.ignore),
            "errors": list(self.errors),
        }


class LocalLinkConnector(SyncConnector):
    source_type = "local_link"
    supports_progress = False

    def resolve_config(self, row) -> dict[str, Any]:
        return {"ignore": parse_patterns(row.ll_ignore)}

    async def sync(
        self,
        *,
        folder_root: Path,
        ignore: list[str] | None = None,
    ) -> LocalLinkSyncStats:
        """Confirm the linked directory is usable; the rescan does the rest."""
        root = folder_root.expanduser()
        if not root.is_dir():
            raise RuntimeError(
                f"linked directory is missing or not a directory: {root}"
            )
        if not os.access(root, os.R_OK | os.X_OK):
            raise RuntimeError(f"linked directory is not readable: {root}")
        return LocalLinkSyncStats(path=str(root), ignore=list(ignore or []))


__all__ = ["LocalLinkConnector", "LocalLinkSyncStats"]
