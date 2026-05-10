"""MIME-keyed dispatch for native Drive exporters.

Adding a new Workspace integration is one line in :func:`build_default_registry`
plus the exporter module itself. The connector consults the registry and
silently skips MIME types not on it (same behaviour as before for niche
types like Sites and Apps Script).
"""

from __future__ import annotations

from functools import lru_cache

from .base import NativeDriveExporter


class ExporterRegistry:
    """First-match dispatch keyed by Drive ``mimeType``.

    Exporters self-declare which MIME they handle via
    :attr:`NativeDriveExporter.mime_type`. The registry stores them in
    a dict — duplicate registrations replace, which is intentional for
    test fixtures that swap in stubs.
    """

    def __init__(self) -> None:
        self._by_mime: dict[str, NativeDriveExporter] = {}

    def register(self, exporter: NativeDriveExporter) -> None:
        if not exporter.mime_type:
            raise ValueError(
                f"Exporter {type(exporter).__name__} did not declare a mime_type"
            )
        self._by_mime[exporter.mime_type] = exporter

    def find(self, mime: str) -> NativeDriveExporter | None:
        return self._by_mime.get(mime)

    @property
    def mime_types(self) -> tuple[str, ...]:
        return tuple(self._by_mime)


def build_default_registry() -> ExporterRegistry:
    """Wire the production exporter set.

    Local imports keep the dependency graph honest — e.g. a deployment
    that wants to disable Forms support could replace this function.
    Each exporter adds a single line here when it lands.
    """
    from .docs import DocumentExporter
    from .sheets import SpreadsheetExporter

    r = ExporterRegistry()
    r.register(DocumentExporter())
    r.register(SpreadsheetExporter())
    return r


@lru_cache(maxsize=1)
def get_default_registry() -> ExporterRegistry:
    return build_default_registry()
