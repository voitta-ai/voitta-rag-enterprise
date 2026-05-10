"""Single source of truth for "what kind of file is this".

Two classifications, one input model:

* :func:`source_kind` — machine-readable string (``"google_doc"`` /
  ``"pdf"`` / …) attached to ``FileInfo`` on the MCP wire so an LLM
  can branch without parsing URLs or extensions.
* :func:`bucket_label` — human-readable display label
  (``"Google Doc"`` / ``".pdf"`` / …) used by the SPA's by-extension
  sidebar.

Both classifications look at ``source_url`` first (Google Workspace
exports get a distinctive https://docs.google.com/… URL set by the
Drive connector) then fall back to the file extension. Keeping the
two helpers next to each other in one module avoids the bug of having
them drift apart — adding a new Workspace type once updates both.
"""

from __future__ import annotations

from pathlib import Path

from ..db.models import File


# (URL prefix, machine kind, display label). Order matters only for
# documentation; lookups are exact-prefix and the prefixes don't overlap.
_WORKSPACE_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("https://docs.google.com/document/", "google_doc", "Google Doc"),
    ("https://docs.google.com/spreadsheets/", "google_sheet", "Google Sheet"),
    ("https://docs.google.com/presentation/", "google_slides", "Google Slides"),
    ("https://docs.google.com/forms/", "google_form", "Google Form"),
    ("https://docs.google.com/drawings/", "google_drawing", "Google Drawing"),
)


# Coarse buckets for non-Workspace files — the extension family that
# determines which parser handled them. Kept short on purpose: this is
# an LLM hint, not the full mime-type catalog. Anything not listed
# falls into ``"other"`` with the bare extension on the bucket label.
_EXTENSION_KIND: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".ipynb": "ipynb",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".rst": "text",
    ".html": "html",
    ".htm": "html",
    ".svg": "image",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".bmp": "image",
    ".tiff": "image",
}


def _ext_lower(rel_path: str) -> str:
    return Path(rel_path).suffix.lower()


def source_kind(file: File) -> str:
    """Return the machine-readable kind for ``file`` (e.g. ``"google_doc"``).

    Used by the MCP response models so an LLM can branch on the kind
    without parsing source URLs. Falls back to ``"other"`` for files
    whose extension we don't have a curated bucket for — the LLM still
    has ``file_path`` to disambiguate.
    """
    if file.source_url:
        for prefix, kind, _label in _WORKSPACE_BUCKETS:
            if file.source_url.startswith(prefix):
                return kind
    ext = _ext_lower(file.rel_path)
    return _EXTENSION_KIND.get(ext, "other")


def bucket_label(file: File) -> str:
    """Return the display label for the sidebar's by-extension table.

    Workspace files get their friendly Google name; everything else
    gets the lowercased extension (or ``"(no ext)"`` for extensionless
    files, preserving the historic format of the by-extension map).
    """
    if file.source_url:
        for prefix, _kind, label in _WORKSPACE_BUCKETS:
            if file.source_url.startswith(prefix):
                return label
    return _ext_lower(file.rel_path) or "(no ext)"
