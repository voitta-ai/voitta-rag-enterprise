"""Figures and page renders: chunk→image resolution, image bytes, page renders."""

from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...cas import store as cas_store
from ...db.database import session_scope
from ...db.models import Chunk, ChunkImageLink, File, Image
from .common import file_provenance, layout_for_page, read_cas_text, resize_for_response
from .models import ImageInfo, PageImageInfo
from .viewer import Viewer

# Markdown image syntax: ``![alt](path)``. Only the path matters; alt text is
# discarded. Remote URLs are skipped — the resolver is for local sibling
# files written by the Drive connector, not arbitrary remote images.
_MD_IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def get_chunk_images(viewer: Viewer, chunk_id: int) -> list[ImageInfo]:
    """Figures linked to ``chunk_id``: intra-file links, then cross-file
    markdown refs, then (when both are empty) the file's other inline images.
    See the ``get_chunk_images`` MCP tool for the full contract."""
    with session_scope() as s:
        chunk = s.get(Chunk, chunk_id)
        if chunk is None:
            raise ValueError(f"Chunk {chunk_id} not found")
        # Folder-scope the chunk via its owning file (cross-file image refs
        # below resolve to siblings in the SAME folder, so this one gate
        # covers them too).
        file = viewer.require_file(s, chunk.file_id)
        chunk_text = chunk.text or ""

        # (1) Intra-file links. ``score`` carries the chunk-image distance
        # so the caller can prefer the tightest crops.
        intra_rows = list(
            s.execute(
                select(Image, ChunkImageLink.distance)
                .join(ChunkImageLink, ChunkImageLink.image_id == Image.id)
                .where(ChunkImageLink.chunk_id == chunk_id)
                .order_by(ChunkImageLink.distance)
            )
        )
        out: list[ImageInfo] = []
        seen_image_ids: set[int] = set()
        intra_url, intra_kind = file_provenance(s, chunk.file_id)
        for img, distance in intra_rows:
            seen_image_ids.add(img.id)
            out.append(
                ImageInfo(
                    image_id=img.id,
                    file_id=img.file_id,
                    file_path=file.rel_path,
                    image_cas_id=img.image_cas_id,
                    page=img.page,
                    width=img.width,
                    height=img.height,
                    mime=img.mime,
                    kind=img.kind,
                    score=float(distance),
                    source_url=intra_url,
                    source_kind=intra_kind,
                )
            )

        # (2) Cross-file markdown image references within this chunk's text.
        # Cheap probe first: most PDF/DOCX chunks carry no ``![`` token.
        if "![" in chunk_text:
            for img, target in _resolve_image_refs(s, file, chunk_text):
                if img.id in seen_image_ids:
                    continue
                seen_image_ids.add(img.id)
                out.append(_image_info_for_ref(s, img, target, score=0.0))

        # (3) File-level fallback: a Google Doc with 21 chunks and 3 inline
        # images only has the ``![](...)`` line in 3 chunks. ``score=None``
        # distinguishes "elsewhere in this file" from "referenced by this
        # chunk" (0.0) and "intra-file linked with distance" (float).
        if not out:
            for img, target in _file_image_refs(s, file):
                if img.id in seen_image_ids:
                    continue
                seen_image_ids.add(img.id)
                out.append(_image_info_for_ref(s, img, target, score=None))
        return out


def get_image(viewer: Viewer, image_id: int, max_size: int = 420) -> dict:
    """Image bytes (base64) + mime; long edge clamped to ``max_size`` (0 = raw)."""
    with session_scope() as s:
        img = s.get(Image, image_id)
        if img is None:
            raise ValueError(f"Image {image_id} not found")
        # An image is reachable only when its file's folder is visible.
        viewer.require_file(s, img.file_id)
        cas_id = img.image_cas_id
        mime = img.mime or "application/octet-stream"
    try:
        data = cas_store.read_image_blob(cas_id)
    except FileNotFoundError as e:
        raise ValueError(f"Image bytes missing for {image_id}") from e
    data, mime = resize_for_response(data, mime, max_size)
    return {
        "image_id": image_id,
        "mime": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def list_page_images(viewer: Viewer, file_id: int) -> list[PageImageInfo]:
    """Per-page renders (PDF) or, failing that, cross-file inline images
    (Google Workspace exports) with a synthetic 1-indexed ``page``."""
    with session_scope() as s:
        f = viewer.require_file(s, file_id)
        source_url, source_kind = file_provenance(s, file_id)
        rows = list(
            s.execute(
                select(Image)
                .where(Image.file_id == file_id, Image.kind == "page_render")
                .order_by(Image.page, Image.image_index)
            ).scalars()
        )
        if rows:
            return [
                PageImageInfo(
                    image_id=img.id,
                    file_id=img.file_id,
                    page=img.page or 0,
                    width=img.width,
                    height=img.height,
                    mime=img.mime,
                    source_url=source_url,
                    source_kind=source_kind,
                )
                for img in rows
            ]

        # ``page`` is the 1-indexed appearance order in the markdown —
        # Workspace files have no real pagination, and the Drive connector
        # writes refs in slide/section order so it is stable.
        out: list[PageImageInfo] = []
        for i, (img, target) in enumerate(_file_image_refs(s, f), start=1):
            t_url, t_kind = file_provenance(s, target.id)
            out.append(
                PageImageInfo(
                    image_id=img.id,
                    file_id=img.file_id,
                    page=i,
                    width=img.width,
                    height=img.height,
                    mime=img.mime,
                    source_url=t_url,
                    source_kind=t_kind,
                )
            )
        return out


def get_page_image(
    viewer: Viewer,
    file_id: int,
    page: int,
    max_size: int = 420,
    include_layout: bool = True,
) -> dict:
    """Bytes (base64) of the page render for ``(file_id, page)``, optionally
    with that page's layout blocks. Raises ``ValueError`` when no render
    exists."""
    with session_scope() as s:
        file = viewer.require_file(s, file_id)
        img = s.execute(
            select(Image)
            .where(
                Image.file_id == file_id,
                Image.kind == "page_render",
                Image.page == page,
            )
            .limit(1)
        ).scalar_one_or_none()
        if img is None:
            raise ValueError(f"No page render for file_id={file_id} page={page}")
        image_id = img.id
        cas_id = img.image_cas_id
        mime = img.mime or "image/webp"
        file_cas_id = file.file_cas_id if include_layout else None
    try:
        data = cas_store.read_image_blob(cas_id)
    except FileNotFoundError as e:
        raise ValueError(
            f"Page-render bytes missing for file_id={file_id} page={page}"
        ) from e
    data, mime = resize_for_response(data, mime, max_size)
    layout: list[dict] = []
    if include_layout and file_cas_id:
        layout = layout_for_page(file_cas_id, page)
    return {
        "image_id": image_id,
        "file_id": file_id,
        "page": page,
        "mime": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "layout": layout,
    }


def _image_info_for_ref(s: Session, img: Image, target: File, *, score: float | None) -> ImageInfo:
    """ImageInfo for a cross-file referenced image. Provenance is the
    target file's (the sibling the image lives on), not the referencing
    file's."""
    t_url, t_kind = file_provenance(s, target.id)
    return ImageInfo(
        image_id=img.id,
        file_id=img.file_id,
        file_path=target.rel_path,
        image_cas_id=img.image_cas_id,
        page=img.page,
        width=img.width,
        height=img.height,
        mime=img.mime,
        kind=img.kind,
        score=score,
        source_url=t_url,
        source_kind=t_kind,
    )


def _resolve_image_refs(s: Session, owner_file: File, text: str) -> list[tuple[Image, File]]:
    """Resolve every ``![alt](rel/path.png)`` in ``text`` to sibling Image rows.

    References resolve against ``owner_file``'s parent directory (Markdown's
    usual rule) inside the same folder; remote URLs, ``..`` escapes above the
    folder root and not-yet-indexed targets resolve to nothing. De-duplicated
    by target file so a doc referencing the same image twice yields one entry.
    """
    parent = PurePosixPath(owner_file.rel_path).parent
    seen_files: set[int] = set()
    out: list[tuple[Image, File]] = []
    for raw_ref in _MD_IMAGE_REF.findall(text):
        if raw_ref.startswith(("http://", "https://", "//", "data:")):
            continue
        try:
            joined = (parent / raw_ref).as_posix()
        except (ValueError, OSError):
            continue
        # Normalize ``a/./b`` and ``a/../b``; reject ``..`` escapes that
        # would walk above the folder root.
        parts: list[str] | None = []
        for p in PurePosixPath(joined).parts:
            if p == "..":
                if not parts:
                    parts = None
                    break
                parts.pop()
            elif p in ("", "."):
                continue
            else:
                parts.append(p)
        if parts is None:
            continue
        target_rel_path = "/".join(parts)
        if not target_rel_path:
            continue
        target = s.execute(
            select(File).where(
                File.folder_id == owner_file.folder_id,
                File.rel_path == target_rel_path,
            )
        ).scalar_one_or_none()
        if target is None or target.id in seen_files:
            continue
        seen_files.add(target.id)
        images = s.execute(
            select(Image)
            .where(Image.file_id == target.id, Image.kind == "figure")
            .order_by(Image.image_index)
        ).scalars()
        out.extend((img, target) for img in images)
    return out


def _file_image_refs(s: Session, file: File) -> list[tuple[Image, File]]:
    """Every sibling-image reference in ``file``'s stored markdown."""
    if file.file_cas_id is None:
        return []
    text = read_cas_text(file.file_cas_id)
    if not text:
        return []
    return _resolve_image_refs(s, file, text)
