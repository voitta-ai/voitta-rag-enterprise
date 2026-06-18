"""Remote-source sync connectors.

Connectors mirror a remote onto the local filesystem under the folder root.
The watcher then picks up created / modified / deleted files via the
existing pipeline. Each connector is invoked from a queue job of kind
``sync`` so progress, errors, and retries land in the same UI as extract /
embed jobs.

Supported ``source_type`` values: ``github``, ``google_drive``, ``nfs``,
``sharepoint``, ``teams``.
"""

from .base import SyncConnector
from .confluence import ConfluenceConnector
from .github import GitHubConnector
from .google_drive import GoogleDriveConnector
from .jira import JiraConnector
from .nfs import NfsConnector
from .registry import SyncRegistry, get_registry
from .sharepoint import SharePointConnector
from .teams import TeamsConnector

__all__ = [
    "ConfluenceConnector",
    "GitHubConnector",
    "GoogleDriveConnector",
    "JiraConnector",
    "NfsConnector",
    "SharePointConnector",
    "SyncConnector",
    "SyncRegistry",
    "TeamsConnector",
    "get_connector",
    "get_registry",
]


def get_connector(source_type: str) -> SyncConnector:
    """Resolve a connector for ``source_type`` via the registry.

    Thin wrapper kept for call-site stability (``indexing.run_sync`` and
    ``routes/sync.py`` import this name). Dispatch lives in :mod:`registry`;
    adding a connector is a registration there, not an edit here.
    """
    return get_registry().get(source_type)
