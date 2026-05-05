"""Sync connector registry. Concrete connectors land in v2."""

from __future__ import annotations

from .base import SyncConnector

FILESYSTEM_SOURCE = "filesystem"


class SourceRegistry:
    """Lookup table from ``source_type`` → ``SyncConnector``."""

    def __init__(self) -> None:
        self._connectors: dict[str, SyncConnector] = {}

    def register(self, connector: SyncConnector) -> None:
        if connector.source_type == FILESYSTEM_SOURCE:
            raise ValueError(
                f"'{FILESYSTEM_SOURCE}' is reserved for plain local folders"
            )
        if connector.source_type in self._connectors:
            raise ValueError(f"Duplicate source_type: {connector.source_type}")
        self._connectors[connector.source_type] = connector

    def get(self, source_type: str) -> SyncConnector | None:
        return self._connectors.get(source_type)

    def list_types(self) -> list[str]:
        return sorted(self._connectors.keys())


_registry: SourceRegistry | None = None


def get_default_registry() -> SourceRegistry:
    global _registry
    if _registry is None:
        _registry = SourceRegistry()
    return _registry


def reset_registry_cache() -> None:
    global _registry
    _registry = None
