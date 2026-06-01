"""Sync connector contract.

A connector mirrors a remote source (GitHub, Google Drive, SharePoint, Teams,
NFS) into the local filesystem under a folder's root. The watcher then picks up
the new/changed files and the indexing pipeline takes over — connectors never
touch Qdrant or SQLite directly.

This ABC is the single place that defines what a connector *is*. Each concrete
connector lives in its own module and is registered in ``registry.py``; the
core (``indexing.run_sync``, ``routes/sync.py``) dispatches through the registry
and never branches on ``source_type``. To add a sync backend: implement this
contract in a new module and register it — nothing in core changes.

The interface is grown across the connector-refactor phases:
- **Phase 0** (now): ``source_type``, ``supports_progress``, ``sync``.
- **Phase 1**: ``to_out`` / ``apply`` / ``is_configured`` move here from the
  per-type if-else chains in ``routes/sync.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...db.models import FolderSyncSource

# Progress callback bridged into the WS event stream by ``indexing.run_sync``:
# ``progress(phase, done, total, detail)``. Connectors that don't report
# progress (``supports_progress = False``) simply never call it.
ProgressFn = Callable[[str, int, int, "dict | None"], None]

# Connectors return a free-form summary dict (e.g. ``{'branches_synced': 1,
# 'commits_written': 0, 'errors': []}``) that ``run_sync`` logs and surfaces.
SyncStats = dict[str, Any]


class SyncConnector(ABC):
    """Base class every sync connector implements.

    ``source_type`` is the registry key, persisted on
    ``folder_sync_sources.source_type``. ``supports_progress`` tells the core
    whether to thread a ``progress`` callback through ``sync`` — this replaces
    the hardcoded ``if source_type in (...)`` set in ``indexing.run_sync``.
    """

    source_type: str
    supports_progress: bool = False

    @abstractmethod
    async def sync(self, *, folder_root: Path, **config: Any) -> SyncStats:
        """Mirror the remote into ``folder_root`` and return a summary dict.

        ``config`` carries the per-connector settings resolved from the folder's
        sync-source row (plus ``progress_cb`` when ``supports_progress``). The
        signature is intentionally loose (``**config``) — each connector names
        the keys it needs.
        """
        ...

    @abstractmethod
    def resolve_config(self, row: "FolderSyncSource") -> dict[str, Any]:
        """Build the kwargs dict ``sync`` expects from the folder's row.

        Reads only ``row`` fields; must be called while ``row`` is still
        attached to an open session. Excludes ``progress_cb`` — the core adds
        that when ``supports_progress`` is set. This is what lets
        ``indexing.run_sync`` stay free of per-``source_type`` branching.
        """
        ...
