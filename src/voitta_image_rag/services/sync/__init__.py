"""Remote-source sync connectors.

Connectors mirror a remote (currently only GitHub) onto the local filesystem
under the folder root. The watcher then picks up created / modified / deleted
files via the existing pipeline. Each connector is invoked from a queue job
of kind ``sync`` so progress, errors, and retries land in the same UI as
extract / embed jobs.
"""

from .github import GitHubConnector

__all__ = ["GitHubConnector", "get_connector"]


def get_connector(source_type: str):
    if source_type == "github":
        return GitHubConnector()
    raise ValueError(f"unknown sync source_type: {source_type!r}")
