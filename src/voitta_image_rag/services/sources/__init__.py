"""Sync connectors.

Connectors write into the watched filesystem; they never bypass the pipeline.
The watcher then picks up the new/changed files and the indexing pipeline
takes over from there.
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
