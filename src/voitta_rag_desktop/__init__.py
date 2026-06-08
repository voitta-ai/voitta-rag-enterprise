"""Briefcase shim for the Voitta RAG Enterprise macOS menu-bar app.

This package is the bundle entry point. It is intentionally light: it sets up
the writable user-data dir + environment, runs the first-launch installer
(lazy-installing the heavy ML stack + the managed Qdrant binary), then starts
the normal FastAPI server in single-user / managed-Qdrant mode and opens the
browser. The actual application lives in the ``voitta_rag_enterprise`` package,
which is imported only *after* the installer has put its dependencies in place.
"""

from ._version import __version__

__all__ = ["__version__"]
