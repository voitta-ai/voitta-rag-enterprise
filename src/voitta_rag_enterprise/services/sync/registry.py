"""Sync connector registry — ``source_type`` → connector.

Mirrors the established pattern in ``services/parsers/registry.py``. The core
resolves connectors through ``get_registry().get(source_type)`` instead of an
if-else chain, so adding a connector is a one-line registration here (or a
self-registration from its own module) rather than an edit to every dispatch
site.

Connectors are stored as classes and instantiated per ``get()`` — preserving
the original ``get_connector`` semantics (a fresh instance per sync) so no
connector that happens to keep per-run state on ``self`` is affected.
"""

from __future__ import annotations

from functools import lru_cache

from .base import SyncConnector


class SyncRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[SyncConnector]] = {}

    def register(self, connector_cls: type[SyncConnector]) -> None:
        self._classes[connector_cls.source_type] = connector_cls

    def get(self, source_type: str) -> SyncConnector:
        try:
            cls = self._classes[source_type]
        except KeyError:
            raise ValueError(f"unknown sync source_type: {source_type!r}") from None
        return cls()

    def list_types(self) -> list[str]:
        return sorted(self._classes)


@lru_cache(maxsize=1)
def get_registry() -> SyncRegistry:
    """Build the default registry once.

    Local imports keep the (heavy) connector dependencies — git, Google/MS
    client libs — off the import path for callers that only need the registry
    type.
    """
    from .cloud_local import CloudLocalConnector
    from .confluence import ConfluenceConnector
    from .github import GitHubConnector
    from .google_drive import GoogleDriveConnector
    from .jira import JiraConnector
    from .nfs import NfsConnector
    from .sharepoint import SharePointConnector
    from .teams import TeamsConnector

    r = SyncRegistry()
    for cls in (
        CloudLocalConnector,
        ConfluenceConnector,
        GitHubConnector,
        GoogleDriveConnector,
        JiraConnector,
        NfsConnector,
        SharePointConnector,
        TeamsConnector,
    ):
        r.register(cls)
    return r
