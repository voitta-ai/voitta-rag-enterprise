"""Pluggable exporters for native Google Workspace files.

Every Workspace MIME the connector knows how to handle has its own
exporter module here. Adding a new type is two steps: write the
exporter, register it in :mod:`registry`. The connector itself doesn't
branch on MIME types beyond ``vnd.google-apps.folder`` (which is plumbing,
not content) — everything else flows through the registry.
"""

from __future__ import annotations

from .base import (
    ExportContext,
    NativeDriveExporter,
    ProducerContext,
    RemoteEntry,
    safe_filename,
)
from .registry import ExporterRegistry, build_default_registry, get_default_registry

__all__ = [
    "ExportContext",
    "ExporterRegistry",
    "NativeDriveExporter",
    "ProducerContext",
    "RemoteEntry",
    "build_default_registry",
    "get_default_registry",
    "safe_filename",
]
