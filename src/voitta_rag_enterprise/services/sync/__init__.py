"""Remote-source sync connectors.

Connectors mirror a remote onto the local filesystem under the folder root.
The watcher then picks up created / modified / deleted files via the
existing pipeline. Each connector is invoked from a queue job of kind
``sync`` so progress, errors, and retries land in the same UI as extract /
embed jobs.

Supported ``source_type`` values: ``github``, ``google_drive``, ``nfs``.
"""

from .github import GitHubConnector
from .google_drive import GoogleDriveConnector
from .nfs import NfsConnector

__all__ = ["GitHubConnector", "GoogleDriveConnector", "NfsConnector", "get_connector"]


def get_connector(source_type: str):
    if source_type == "github":
        return GitHubConnector()
    if source_type == "google_drive":
        return GoogleDriveConnector()
    if source_type == "nfs":
        return NfsConnector()
    raise ValueError(f"unknown sync source_type: {source_type!r}")
