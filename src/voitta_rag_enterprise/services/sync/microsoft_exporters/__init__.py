"""Microsoft-native content exporters.

Functional modules — each exporter is a few async functions the
SharePoint / Teams connector calls directly. No MIME registry like the
Google exporters need (here each call site already knows which exporter
it wants: ``sharepoint.py`` calls ``onenote.export_notebook(...)``,
``teams.py`` calls ``teams_transcript.export(...)``).

If we ever grow more than one polymorphic dispatch path through here,
factor a registry. Until then, keep it as straight functions.
"""

from .base import (
    FINGERPRINT_PREFIX,
    FINGERPRINT_SUFFIX,
    RemoteEntry,
    atomic_write_text,
    atomic_write_bytes,
    fingerprint_matches,
    fingerprint_header,
    safe_filename,
)

__all__ = [
    "FINGERPRINT_PREFIX",
    "FINGERPRINT_SUFFIX",
    "RemoteEntry",
    "atomic_write_text",
    "atomic_write_bytes",
    "fingerprint_matches",
    "fingerprint_header",
    "safe_filename",
]
