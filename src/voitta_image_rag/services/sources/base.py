"""Sync connector contract.

A connector pulls remote content (Google Drive, GitHub, Confluence, …) and
writes it into a local directory under a registered folder. The watcher then
picks up the new/changed files and the indexing pipeline takes over from there.

Connectors **never** write directly to Qdrant or SQLite. This keeps the
downstream pipeline source-agnostic.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class SyncResult:
    """Summary of one sync run, surfaced to the user."""

    source_type: str
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    errors: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def changed(self) -> int:
        return self.files_added + self.files_updated + self.files_removed


class SyncConnector(Protocol):
    """Pull remote content into a local directory the watcher already observes."""

    source_type: str
    """Stable identifier (e.g. ``"gdrive"``). Persisted on ``Folder.source_type``."""

    def validate_config(self, config: dict) -> None:
        """Raise on a malformed connector-specific config dict."""
        ...

    async def sync(self, dest_dir: Path, config: dict) -> SyncResult:
        """Materialise/refresh remote content under ``dest_dir``.

        Implementations should also write ``.voitta_sources.json`` at
        ``dest_dir`` so the indexer can populate ``files.source_url``.
        """
        ...


# Type alias for a sync-callable for non-class connectors.
SyncCallable = Awaitable[SyncResult]
