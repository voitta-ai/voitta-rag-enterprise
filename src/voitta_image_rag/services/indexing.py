"""End-to-end ``extract`` job handler.

See ARCHITECTURE.md §4.3 (reindex strategy) and §4.5 (image-chunk linkage).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from sqlalchemy import select

from ..cas import store as cas_store
from ..config import get_settings
from ..db.database import session_scope
from ..db.models import Chunk, ChunkImageLink, File, Folder, Image
from . import events, job_queue
from .chunking import ChunkInfo, anchor_chunk_for_position, chunk_markdown
from .parsers.registry import get_default_registry

logger = logging.getLogger(__name__)


async def run_extract(payload: dict) -> None:
    file_id = int(payload["file_id"])
    await asyncio.to_thread(_run_extract_sync, file_id)


def _run_extract_sync(file_id: int) -> None:
    abs_path = _resolve_path(file_id)
    if abs_path is None:
        return  # file or folder gone; ``delete_file`` cleans up

    if not abs_path.exists() or not abs_path.is_file():
        _mark_state(file_id, state="deleted")
        return

    settings = get_settings()
    try:
        stat = abs_path.stat()
    except OSError as e:
        _mark_error(file_id, f"stat failed: {e}")
        return
    if stat.st_size > settings.max_file_bytes:
        _mark_error(file_id, "size exceeds VOITTA_MAX_FILE_BYTES")
        return

    raw = abs_path.read_bytes()
    new_sha = hashlib.sha256(raw).hexdigest()

    if _short_circuit_unchanged(file_id, new_sha, stat.st_mtime_ns):
        return

    parser = get_default_registry().find(abs_path)
    if parser is None:
        _mark_error(file_id, f"no parser for {abs_path.suffix}")
        return

    result = parser.parse(abs_path)
    if not result.success:
        _mark_error(file_id, result.error or "parse failed")
        return

    cas_store.write_file_blob(new_sha, "text.md", result.content)
    image_shas: list[str] = [cas_store.write_image_blob(img.bytes) for img in result.images]
    chunks = chunk_markdown(result.content)

    cas_store.write_file_blob(
        new_sha,
        "manifest.json",
        json.dumps(
            {
                "parser": parser.__class__.__name__,
                "chunk_count": len(chunks),
                "image_count": len(result.images),
                "image_positions": [img.position for img in result.images],
                "metadata": result.metadata,
            },
            indent=2,
        ),
    )

    _commit_indexing(
        file_id=file_id,
        new_sha=new_sha,
        mtime_ns=stat.st_mtime_ns,
        chunks=chunks,
        images=list(zip(image_shas, result.images, strict=True)),
        nearby_radius=settings.nearby_radius,
    )


def _resolve_path(file_id: int) -> Path | None:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return None
        folder = s.get(Folder, file.folder_id)
        if folder is None:
            return None
        return Path(folder.path) / file.rel_path


def _short_circuit_unchanged(file_id: int, new_sha: str, mtime_ns: int) -> bool:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None or file.file_cas_id != new_sha:
            return False
        file.mtime_ns = mtime_ns
        file.last_seen_at = int(time.time())
        return True


def _mark_state(file_id: int, *, state: str, error: str | None = None) -> None:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        file.state = state
        file.error = error
    _publish_file_upserted(file_id)


def _publish_file_upserted(file_id: int) -> None:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        events.publish(
            "files",
            {
                "type": "file.upserted",
                "file": _file_event_payload(file),
            },
        )


def _file_event_payload(file: File) -> dict:
    return {
        "id": file.id,
        "folder_id": file.folder_id,
        "rel_path": file.rel_path,
        "state": file.state,
        "size_bytes": file.size_bytes,
        "mtime_ns": file.mtime_ns,
        "last_indexed_at": file.last_indexed_at,
        "pending_embeds": file.pending_embeds,
        "source_url": file.source_url,
    }


def _mark_error(file_id: int, message: str) -> None:
    logger.warning("extract %d failed: %s", file_id, message)
    _mark_state(file_id, state="error", error=message)


def _commit_indexing(
    *,
    file_id: int,
    new_sha: str,
    mtime_ns: int,
    chunks: list[ChunkInfo],
    images: list[tuple[str, object]],  # (sha, ExtractedImage)
    nearby_radius: int,
) -> None:
    now = int(time.time())
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return

        old_sha = file.file_cas_id

        # Decref every image attached to the previous extract; delete chunk/image rows.
        old_images = list(s.execute(select(Image).where(Image.file_id == file_id)).scalars())
        for img in old_images:
            cas_store.decref(s, cas_store.KIND_IMAGE, img.image_cas_id)
        for img in old_images:
            s.delete(img)
        for ch in list(s.execute(select(Chunk).where(Chunk.file_id == file_id)).scalars()):
            s.delete(ch)
        s.flush()

        if old_sha is not None and old_sha != new_sha:
            cas_store.decref(s, cas_store.KIND_FILE, old_sha)
        if old_sha != new_sha:
            cas_store.incref(s, cas_store.KIND_FILE, new_sha)

        chunk_objs: list[Chunk] = []
        for i, ci in enumerate(chunks):
            ch = Chunk(
                file_id=file_id,
                chunk_index=i,
                chunk_hash=hashlib.sha256(ci.text.encode("utf-8")).hexdigest(),
                text=ci.text,
                char_start=ci.char_start,
                char_end=ci.char_end,
                created_at=now,
            )
            s.add(ch)
            chunk_objs.append(ch)
        s.flush()

        for i, (sha, img_data) in enumerate(images):
            anchor = anchor_chunk_for_position(img_data.position, chunks)
            image = Image(
                file_id=file_id,
                image_index=i,
                image_cas_id=sha,
                anchor_chunk=anchor,
                page=img_data.page,
                width=img_data.width,
                height=img_data.height,
                mime=img_data.mime,
                created_at=now,
            )
            s.add(image)
            cas_store.incref(s, cas_store.KIND_IMAGE, sha)
            s.flush()

            if anchor is not None:
                for ch in chunk_objs:
                    distance = abs(ch.chunk_index - anchor)
                    if distance <= nearby_radius:
                        s.add(
                            ChunkImageLink(
                                chunk_id=ch.id, image_id=image.id, distance=distance
                            )
                        )

        file.file_cas_id = new_sha
        file.mtime_ns = mtime_ns
        file.last_seen_at = now
        file.last_indexed_at = now
        file.state = "extracted"
        file.error = None
        file.pending_embeds = (1 if chunks else 0) + len(images)
        _committed_file_id = file.id

        if chunks:
            job_queue.enqueue(
                s, "embed_text", {"file_id": file_id}, dedup_key=f"embed_text:{file_id}"
            )
        for image in s.execute(select(Image).where(Image.file_id == file_id)).scalars():
            job_queue.enqueue(
                s,
                "embed_image",
                {"image_id": image.id},
                dedup_key=f"embed_image:{image.id}",
            )

    _publish_file_upserted(_committed_file_id)


async def run_embed_text(payload: dict) -> None:
    file_id = int(payload["file_id"])
    try:
        await asyncio.to_thread(_embed_text_sync, file_id)
    except Exception as e:
        _mark_file_error_for_text(file_id, f"embed_text failed: {e}")
        raise


async def run_embed_image(payload: dict) -> None:
    image_id = int(payload["image_id"])
    try:
        await asyncio.to_thread(_embed_image_sync, image_id)
    except Exception as e:
        _mark_file_error_for_image(image_id, f"embed_image failed: {e}")
        raise


async def run_delete_file(payload: dict) -> None:
    file_id = int(payload["file_id"])
    await asyncio.to_thread(_delete_file_sync, file_id)


def _embed_text_sync(file_id: int) -> None:
    from . import vector_store
    from .acl import allowed_user_ids_for_file
    from .embedding import get_sparse_embedder, get_text_embedder

    settings = get_settings()
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        chunks = list(
            s.execute(select(Chunk).where(Chunk.file_id == file_id).order_by(Chunk.chunk_index))
            .scalars()
        )
        chunk_data = [
            (c.id, c.text, c.chunk_index, _nearby_image_ids(s, c.id)) for c in chunks
        ]
        folder_id = file.folder_id
        rel_path = file.rel_path
        source_url = file.source_url
        allowed_users = allowed_user_ids_for_file(s, file_id)

    text_emb = get_text_embedder()
    sparse_emb = get_sparse_embedder()
    vector_store.ensure_chunks_collection(text_dim=text_emb.dim)

    if chunk_data:
        texts = [t for _, t, _, _ in chunk_data]
        denses = text_emb.embed_documents(texts)
        sparses = sparse_emb.embed_documents(texts)
        points = [
            vector_store.ChunkPoint(
                chunk_id=cid,
                file_id=file_id,
                folder_id=folder_id,
                file_path=rel_path,
                chunk_index=idx,
                text=text,
                dense=dense,
                sparse=sparse,
                nearby_image_ids=nearby,
                source_url=source_url,
                dense_model_version=settings.dense_version,
                sparse_model_version=settings.sparse_version,
                allowed_users=allowed_users,
            )
            for (cid, text, idx, nearby), dense, sparse in zip(
                chunk_data, denses, sparses, strict=True
            )
        ]
    else:
        points = []

    vector_store.replace_chunks_for_file(file_id, points)
    _decrement_pending_embeds(file_id)


def _embed_image_sync(image_id: int) -> None:
    from ..cas import store as cas_store
    from . import vector_store
    from .acl import allowed_user_ids_for_file
    from .embedding import get_image_embedder

    settings = get_settings()
    with session_scope() as s:
        image = s.get(Image, image_id)
        if image is None:
            return
        file_id = image.file_id
        cas_id = image.image_cas_id
        anchor = image.anchor_chunk
        page = image.page
        file = s.get(File, file_id)
        if file is None:
            return
        folder_id = file.folder_id
        rel_path = file.rel_path
        allowed_users = allowed_user_ids_for_file(s, file_id)

    image_emb = get_image_embedder()
    vector_store.ensure_images_collection(image_dim=image_emb.dim)

    existing = vector_store.find_image_point_by_cas(cas_id)
    if existing is not None:
        vector_store.add_file_to_image_point(existing["id"], file_id)
    else:
        data = cas_store.read_image_blob(cas_id)
        vec = image_emb.embed_image(data)
        vector_store.upsert_image_point(
            vector_store.ImagePoint(
                point_id=image_id,
                image_cas_id=cas_id,
                file_id=file_id,
                folder_id=folder_id,
                file_path=rel_path,
                anchor_chunk=anchor,
                page=page,
                image=vec,
                image_model_version=settings.image_version,
                allowed_users=allowed_users,
            ),
            file_ids=[file_id],
        )

    _decrement_pending_embeds(file_id)


def _delete_file_sync(file_id: int) -> None:
    from ..cas import store as cas_store
    from . import vector_store

    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        old_sha = file.file_cas_id
        old_images = list(s.execute(select(Image).where(Image.file_id == file_id)).scalars())
        for img in old_images:
            cas_store.decref(s, cas_store.KIND_IMAGE, img.image_cas_id)
        if old_sha is not None:
            cas_store.decref(s, cas_store.KIND_FILE, old_sha)
        # SQLAlchemy CASCADE on file row will drop chunks/images/links.
        s.delete(file)

    vector_store.delete_chunks_for_file(file_id)
    vector_store.remove_file_from_image_points(file_id)
    events.publish("files", {"type": "file.deleted", "file_id": file_id})


def _decrement_pending_embeds(file_id: int) -> None:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        file.pending_embeds = max(0, file.pending_embeds - 1)
        if file.pending_embeds == 0 and file.state in ("extracted", "embedding"):
            file.state = "indexed"
    _publish_file_upserted(file_id)


def _mark_file_error_for_image(image_id: int, message: str) -> None:
    """Called by the worker when ``embed_image`` fails — propagate to the file."""
    with session_scope() as s:
        image = s.get(Image, image_id)
        if image is None:
            return
        file_id = image.file_id
    _mark_state(file_id, state="error", error=message)


def _mark_file_error_for_text(file_id: int, message: str) -> None:
    _mark_state(file_id, state="error", error=message)


def _nearby_image_ids(session, chunk_id: int) -> list[int]:
    return [
        link.image_id
        for link in session.execute(
            select(ChunkImageLink).where(ChunkImageLink.chunk_id == chunk_id)
        )
        .scalars()
        .all()
    ]


HANDLERS = {
    "extract": run_extract,
    "embed_text": run_embed_text,
    "embed_image": run_embed_image,
    "delete_file": run_delete_file,
}
