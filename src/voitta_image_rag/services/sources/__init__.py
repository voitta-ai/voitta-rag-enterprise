"""Sync connectors.

Connectors write into the watched filesystem; they never bypass the pipeline.
See ARCHITECTURE.md §10. v1 ships only the base contract + registry; concrete
connectors (Google Drive, GitHub, …) land in v2.
"""

from .base import SyncConnector, SyncResult
from .registry import (
    FILESYSTEM_SOURCE,
    SourceRegistry,
    get_default_registry,
    reset_registry_cache,
)

__all__ = [
    "FILESYSTEM_SOURCE",
    "SourceRegistry",
    "SyncConnector",
    "SyncResult",
    "get_default_registry",
    "reset_registry_cache",
]
