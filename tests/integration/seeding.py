"""Seed an indexed, owned folder for integration tests.

Runs the real extract + embed pipeline (with the fake embedders the ``env``
fixture configures) so retrieval/MCP/assistant tests exercise genuine
chunk, image and Qdrant rows.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image as PILImage
from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, File, Folder, Image
from voitta_rag_enterprise.services.acl import get_or_create_user
from voitta_rag_enterprise.services.indexing import (
    run_embed_image,
    run_embed_text,
    run_extract,
)


def png(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def seed_owned(root: Path, owner_email: str, layout: dict, source_url: str | None = None) -> dict:
    """Index ``layout`` into a folder OWNED by ``owner_email``'s account.

    Returns ids the tests probe: folder_id, a text file id, its first
    chunk id, and an image id.
    """
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content) if isinstance(content, str) else p.write_bytes(content)
    init_db()
    with session_scope() as s:
        owner = get_or_create_user(s, owner_email)
        folder = Folder(path=str(root), display_name=root.name, owner_id=owner.id)
        s.add(folder)
        s.flush()
        folder_id = folder.id
        for rel in layout:
            st = (root / rel).stat()
            s.add(File(
                folder_id=folder_id, rel_path=rel, size_bytes=st.st_size,
                mtime_ns=st.st_mtime_ns, last_seen_at=0, state="pending",
                source_url=(source_url if rel.endswith(".md") else None),
            ))
    with session_scope() as s:
        ids = [f.id for f in s.execute(
            select(File).where(File.folder_id == folder_id)).scalars()]
    for fid in ids:
        asyncio.run(run_extract({"file_id": fid}))
        asyncio.run(run_embed_text({"file_id": fid}))
        with session_scope() as s:
            for img in s.execute(select(Image).where(Image.file_id == fid)).scalars():
                asyncio.run(run_embed_image({"image_id": img.id}))
    with session_scope() as s:
        text_file = s.execute(
            select(File).where(File.folder_id == folder_id, File.rel_path.like("%.md"))
        ).scalars().first()
        chunk = s.execute(
            select(Chunk).where(Chunk.file_id == text_file.id)
        ).scalars().first()
        image = s.execute(
            select(Image).join(File, Image.file_id == File.id)
            .where(File.folder_id == folder_id)
        ).scalars().first()
        return {
            "folder_id": folder_id,
            "file_id": text_file.id,
            "chunk_id": chunk.id if chunk else None,
            "image_id": image.id if image else None,
        }
