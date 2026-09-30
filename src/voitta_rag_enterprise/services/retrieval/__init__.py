"""Retrieval over the index — the logic behind the MCP tools and the assistant.

Every function takes an explicit :class:`Viewer` (who is reading and which
folder scope applies) and enforces folder visibility itself, so a caller
cannot forget the ACL: id-taking functions route through
``Viewer.require_file``; searches through ``Viewer.search_filter``.

Callers:

* ``mcp_server`` — thin ``@mcp.tool`` wrappers; builds the Viewer from the
  bearer-authenticated account, scope ``"mcp_active"``.
* ``services.assistant.tools`` — builds it from the (impersonation-aware)
  WebSocket identity, scope ``"visible"``.

Module layout: ``viewer`` (identity + scope), ``models`` (wire models),
``browse`` / ``search`` / ``content`` / ``images`` / ``assets`` (one per
concern), ``common`` (provenance, layout and image-bytes helpers).
"""

from .assets import list_assets, request_asset
from .browse import list_indexed_folders, resolve_url
from .content import get_chunk_range, get_file, get_page_layout, get_workbook
from .images import get_chunk_images, get_image, get_page_image, list_page_images
from .models import ChunkInfo, EntryInfo, FileInfo, FolderInfo, ImageInfo, PageImageInfo
from .search import chunk_from_hit, image_from_hit, search, search_images
from .viewer import FolderScope, Viewer

__all__ = [
    "ChunkInfo",
    "EntryInfo",
    "FileInfo",
    "FolderInfo",
    "FolderScope",
    "ImageInfo",
    "PageImageInfo",
    "Viewer",
    "chunk_from_hit",
    "get_chunk_images",
    "get_chunk_range",
    "get_file",
    "get_image",
    "get_page_image",
    "get_page_layout",
    "get_workbook",
    "image_from_hit",
    "list_assets",
    "list_indexed_folders",
    "list_page_images",
    "request_asset",
    "resolve_url",
    "search",
    "search_images",
]
