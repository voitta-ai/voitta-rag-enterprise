"""Read indexed content: whole-file markdown, chunk ranges, page layout, workbooks."""

from __future__ import annotations

import base64
from pathlib import Path

from sqlalchemy import select

from ...cas import store as cas_store
from ...db.database import session_scope
from ...db.models import Chunk, Folder
from ..file_classify import source_kind as classify_source_kind
from .common import file_info, layout_for_page, nearby_image_ids
from .models import ChunkInfo
from .viewer import Viewer

# Hard cap on one chunk-range read; paging a whole file through context is a
# sign the caller should process the original bytes instead.
MAX_CHUNK_RANGE = 500


def get_file(viewer: Viewer, file_id: int) -> dict:
    """File metadata + the full extracted markdown (``text``)."""
    with session_scope() as s:
        f = viewer.require_file(s, file_id)
        info = file_info(f)
        cas_id = f.file_cas_id

    text = ""
    if cas_id:
        try:
            text = cas_store.read_file_blob(cas_id, "text.md").decode("utf-8")
        except FileNotFoundError:
            text = ""
    return {"file": info.model_dump(), "text": text}


def get_chunk_range(
    viewer: Viewer, file_id: int, start: int = 0, end: int = 10
) -> list[ChunkInfo]:
    """Chunks ``[start, end)`` of a file in order, with page anchoring."""
    from ..indexing import _load_char_to_page, _load_layout_summaries
    from ..layout import pages_for_range, primary_page_for_range

    start_index = max(0, start)
    end_index = min(end, start_index + MAX_CHUNK_RANGE)
    if end_index <= start_index:
        return []
    with session_scope() as s:
        f = viewer.require_file(s, file_id)
        file_cas_id = f.file_cas_id
        rel_path = f.rel_path
        f_source_url = f.source_url
        f_source_kind = classify_source_kind(f)
        rows = list(
            s.execute(
                select(Chunk)
                .where(
                    Chunk.file_id == file_id,
                    Chunk.chunk_index >= start_index,
                    Chunk.chunk_index < end_index,
                )
                .order_by(Chunk.chunk_index)
            ).scalars()
        )
        chunk_data = [
            (c.id, c.chunk_index, c.text, c.char_start, c.char_end, nearby_image_ids(s, c.id))
            for c in rows
        ]

    char_to_page = _load_char_to_page(file_cas_id)
    layout_summaries = _load_layout_summaries(file_cas_id)
    out: list[ChunkInfo] = []
    for cid, idx, text, c_start, c_end, nearby in chunk_data:
        primary = (
            primary_page_for_range(char_to_page, c_start or 0, c_end or 0)
            if char_to_page
            else None
        )
        pages = (
            pages_for_range(char_to_page, c_start or 0, c_end or (c_start or 0) + 1)
            if char_to_page
            else []
        )
        out.append(
            ChunkInfo(
                chunk_id=cid,
                file_id=file_id,
                file_path=rel_path,
                chunk_index=idx,
                text=text,
                nearby_image_ids=nearby,
                page=primary,
                pages=pages,
                layout=layout_summaries.get(primary) if primary else None,
                source_url=f_source_url,
                source_kind=f_source_kind,
            )
        )
    return out


def get_page_layout(viewer: Viewer, file_id: int, page: int) -> list[dict]:
    """The parser's per-block layout list for ``(file_id, page)`` (1-indexed)."""
    with session_scope() as s:
        file = viewer.require_file(s, file_id)
        file_cas_id = file.file_cas_id
    if not file_cas_id:
        return []
    return layout_for_page(file_cas_id, page)


def get_workbook(viewer: Viewer, file_id: int) -> dict:
    """The ``.xlsx`` behind a Sheets-derived per-sheet markdown file, base64."""
    with session_scope() as s:
        f = viewer.require_file(s, file_id)
        rel_path = Path(f.rel_path)
        folder = s.get(Folder, f.folder_id)
        if folder is None:
            raise ValueError(f"File {file_id} has no folder")
        folder_path = Path(folder.path)

    # Per-sheet md path: ``<some-dir>/<workbook stem>/NN-<sheet>.md``.
    # Workbook xlsx: ``.voitta_workbooks/<some-dir>/<workbook stem>.xlsx``.
    if rel_path.suffix.lower() != ".md" or rel_path.parent == Path(""):
        raise ValueError(
            f"File {file_id} ({rel_path}) is not a Sheets-derived markdown file"
        )
    workbook_rel = rel_path.parent  # e.g. "MyFolder/Q4 Plan"
    xlsx_path = folder_path / ".voitta_workbooks" / workbook_rel.with_suffix(".xlsx")
    if not xlsx_path.exists():
        raise FileNotFoundError(
            f"Workbook xlsx not found at {xlsx_path}. The folder may need to be "
            f"re-synced — pre-exporter syncs didn't write the .voitta_workbooks "
            f"sidecar."
        )

    data = xlsx_path.read_bytes()
    return {
        "file_id": file_id,
        "filename": xlsx_path.name,
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "data_base64": base64.b64encode(data).decode("ascii"),
        "size_bytes": len(data),
    }
