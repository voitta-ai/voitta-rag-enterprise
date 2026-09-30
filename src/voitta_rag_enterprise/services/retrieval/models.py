"""Wire models for retrieval results.

Shared by the MCP tools (FastMCP advertises their JSON schema to clients)
and the in-app assistant's tools. Trimmed for LLM friendliness compared to
the REST responses.

Field-presence policy across every model: declared optional fields always
appear on the wire with their declared value (``null``/empty for unknowns).
No custom serializer strips them, so the advertised JSON schema matches the
wire format byte-for-byte and client-side structured-content validators
don't have to special-case "absent vs null". Adding a new field means
giving it a default — never a serializer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FolderInfo(BaseModel):
    id: int
    path: str
    display_name: str
    source_type: str
    files_total: int
    files_indexed: int
    # True when this folder is currently included in the caller's MCP search
    # (i.e. they haven't toggled it off in Settings). Always True in single-
    # user / dev-user modes.
    active: bool = True
    # True when the folder is shared globally (owner toggled the switch).
    shared: bool = False


class EntryInfo(BaseModel):
    """A single entry returned when ``list_indexed_folders`` is called with a
    non-empty ``prefix`` — i.e. when the tool is being used as a directory
    listing rather than a roots listing."""

    # ``"folder"`` for a (sub)directory, ``"file"`` for an indexed file.
    kind: str
    # Last path segment (the filename or subdir name).
    name: str
    # Full virtual path from the storage root, e.g. ``"MyDocs/sub/file.pdf"``.
    # Folder entries end with ``/``.
    path: str
    # Containing top-level folder id, so callers can pivot to other tools
    # (search with folder_ids=[…], get_file, …) without a second lookup.
    folder_id: int
    # File-only fields — None for folder entries.
    file_id: int | None = None
    state: str | None = None
    size_bytes: int | None = None
    source_url: str | None = None
    source_kind: str | None = None


class FileInfo(BaseModel):
    id: int
    folder_id: int
    rel_path: str
    state: str
    # Source URL set by the sync connectors (Google Drive deep-link,
    # GitHub raw URL, …). None for files indexed from a local path.
    source_url: str | None = None
    # Unix epoch seconds of the last successful indexing pass. None
    # for files that have never reached state='indexed'.
    last_indexed_at: int | None = None
    # Coarse classifier for an LLM that wants to branch on what kind of
    # source this is — ``"google_doc"`` / ``"google_sheet"`` /
    # ``"google_slides"`` / ``"google_form"`` / ``"google_drawing"`` for
    # Drive exports (classified by source_url), and ``"pdf"`` / ``"docx"``
    # / ``"pptx"`` / ``"xlsx"`` / ``"ipynb"`` / ``"markdown"`` / ``"text"``
    # / ``"html"`` / ``"image"`` / ``"other"`` for everything else
    # (classified by extension). See ``services/file_classify.py`` for
    # the full table.
    source_kind: str = "other"


class ChunkInfo(BaseModel):
    # ``"chunk"`` for a document chunk; ``"folder_card"`` for a synthetic
    # folder/subfolder hit (name + optional description matched the query).
    # A folder_card has NO file: ``file_id`` is null, ``chunk_id`` is 0 and
    # ``file_path`` holds the subfolder path inside the folder ('' = the
    # folder root). Do not call get_file / get_chunk_range on it — instead
    # scope a follow-up search with folder_ids=[folder_id].
    kind: str = "chunk"
    chunk_id: int
    # Null only on folder_card hits (see ``kind``); always an int for chunks.
    file_id: int | None
    # Owning folder — set on every hit so callers can pivot to
    # search(folder_ids=[…]) without a lookup.
    folder_id: int | None = None
    file_path: str
    chunk_index: int
    text: str
    # Image ids whose extracted figure overlaps this chunk's page span.
    # Empty list for non-PDF chunks (only the PDF pipeline links chunks
    # to figures); the field is always emitted.
    nearby_image_ids: list[int] = Field(default_factory=list)
    # Search hit score (dense + sparse RRF). None on chunk-range / get-file
    # responses where there's no ranking context.
    score: float | None = None
    # Page anchoring + layout summary, attached at index time. ``page``
    # is the chunk's primary (start-anchored) page; ``pages`` is every
    # page the chunk touches. ``layout`` is the per-page summary dict
    # (``layout_kind``, ``layout_has_image``/``_table``, ``layout_n_*``,
    # …) — mirror of what search filters can match on. All None / empty
    # for chunks from non-PDF parsers (text/code/markdown/...).
    page: int | None = None
    pages: list[int] = Field(default_factory=list)
    layout: dict | None = None
    # File-level provenance, mirrored onto every chunk hit so an LLM can
    # deep-link to the canonical source (Google doc URL, GitHub raw URL,
    # …) without a follow-up ``get_file`` call. ``source_kind`` is the
    # same machine classifier carried on ``FileInfo``.
    source_url: str | None = None
    source_kind: str = "other"


class ImageInfo(BaseModel):
    image_id: int
    file_id: int
    file_path: str
    image_cas_id: str
    # PDF page number this figure sits on. None for figures from non-PDF
    # parsers (DOCX/PPTX images, standalone uploads) and for search hits
    # where the indexer hasn't stored a page reference.
    page: int | None = None
    # Pixel dimensions + mime are stored on the Image DB row; search
    # payloads don't carry them, so they're None on search hits and
    # populated on metadata-direct paths (get_chunk_images, list_page_images).
    width: int | None = None
    height: int | None = None
    mime: str | None = None
    # 'figure' (cropped extract) or 'page_render' (full-page raster).
    # search_images / get_chunk_images return only figures; page renders
    # are surfaced via list_page_images / get_page_image.
    kind: str = "figure"
    # Search hit score. None on non-search paths.
    score: float | None = None
    # Per-page layout summary for the page this image sits on. None for
    # images that don't come from the PDF pipeline.
    layout: dict | None = None
    # File-level provenance — see ChunkInfo for the rationale.
    source_url: str | None = None
    source_kind: str = "other"


class PageImageInfo(BaseModel):
    """Catalog entry for a per-page render. Bytes are fetched separately."""

    image_id: int
    file_id: int
    page: int
    # Width / height / mime are stored on the Image row in DB; these
    # endpoints read from there, so the values are normally populated.
    # Kept nullable to cover legacy rows pre-dating the metadata write.
    width: int | None = None
    height: int | None = None
    mime: str | None = None
    # File-level provenance — see ChunkInfo.
    source_url: str | None = None
    source_kind: str = "other"
