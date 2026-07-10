"""Text + image embed pipelines and their worker handlers.

Runs the SigLIP/e5 forward passes, upserts Qdrant points, and decrements the
per-file pending-embed counter. Invoked both by the job queue (``embed_text`` /
``embed_image``) and inline from ``extract``.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from ...config import get_settings
from ...db.database import session_scope
from ...db.models import Chunk, ChunkImageLink, File, Image
from ...logging_config import bind_context
from .accounting import _decrement_pending_embeds
from .common import (
    _format_exception,
    _mark_state,
    _publish_job_progress,
    _stage,
    logger,
)
from .layout import _load_char_to_page, _load_layout_summaries

# Chunks per embed batch for per-batch progress emission. Sentence-transformers
# batches internally anyway, so splitting the call site adds negligible
# overhead but gives the Jobs panel a moving counter on large files.
_EMBED_PROGRESS_BATCH = 256


def _build_meta_payload(
    *,
    source_meta: str | None,
    source_url: str | None,
    added_at: int | None,
    mtime_ns: int | None,
) -> dict | None:
    """Assemble the flat ``meta_*`` payload for a file's chunk/image points.

    Single source of truth: ``File.source_meta`` (JSON a connector captured) →
    owner/editor/shared_by + created/modified epochs, via
    ``source_meta.payload_fields``. ``added_at`` becomes ``meta_uploaded_ts``.

    Filesystem ``mtime`` is used for ``meta_modified_ts`` **only for non-synced
    files** (``source_url is None`` — local uploads / NFS / github), where it's
    the real modified time. For synced files the source date is authoritative;
    we never fall back to fs-mtime there because that's just the *download*
    time, which would be wrong.
    """
    import json as _json

    from .. import source_meta as sm

    parsed: dict | None = None
    if source_meta:
        try:
            parsed = _json.loads(source_meta)
        except (ValueError, TypeError):
            parsed = None

    fs_modified = (
        int(mtime_ns // 1_000_000_000)
        if (source_url is None and isinstance(mtime_ns, int))
        else None
    )
    return sm.payload_fields(
        parsed, uploaded_ts=added_at, modified_fallback_ts=fs_modified
    ) or None


async def run_embed_text(payload: dict) -> None:
    file_id = int(payload["file_id"])
    round_token = payload.get("round")
    try:
        await asyncio.to_thread(_embed_text_sync, file_id, round_token)
    except Exception:
        with bind_context(file_id=file_id):
            logger.exception("embed_text failed")
        _mark_file_error_for_text(file_id, _format_exception("embed_text failed"))
        raise


async def run_embed_image(payload: dict) -> None:
    image_id = int(payload["image_id"])
    round_token = payload.get("round")
    try:
        await asyncio.to_thread(_embed_image_sync, image_id, round_token)
    except Exception:
        with bind_context(image_id=image_id):
            logger.exception("embed_image failed")
        _mark_file_error_for_image(image_id, _format_exception("embed_image failed"))
        raise


def _embed_text_sync(file_id: int, round_token: int | None = None) -> None:
    from .. import vector_store
    from ..acl import allowed_user_ids_for_file
    from ..embedding import get_sparse_embedder, get_text_embedder
    from ..layout import pages_for_range, primary_page_for_range

    with bind_context(file_id=file_id, round=round_token):
        logger.info("embed_text begin")
        settings = get_settings()
        with _stage("embed_text.load_chunks"), session_scope() as s:
            file = s.get(File, file_id)
            if file is None:
                logger.info("embed_text abort: file gone")
                return
            if round_token is not None and file.embed_round != round_token:
                logger.info(
                    "embed_text abort: stale round (job=%s file=%s)",
                    round_token,
                    file.embed_round,
                )
                return
            chunks = list(
                s.execute(
                    select(Chunk)
                    .where(Chunk.file_id == file_id)
                    .order_by(Chunk.chunk_index)
                ).scalars()
            )
            chunk_data = [
                (
                    c.id,
                    c.text,
                    c.chunk_index,
                    _nearby_image_ids(s, c.id),
                    c.char_start,
                    c.char_end,
                )
                for c in chunks
            ]
            folder_id = file.folder_id
            rel_path = file.rel_path
            source_url = file.source_url
            tab = file.tab
            file_cas_id = file.file_cas_id
            file_source_meta = file.source_meta
            file_added_at = file.added_at
            file_mtime_ns = file.mtime_ns
            allowed_users = allowed_user_ids_for_file(s, file_id)

        char_to_page = _load_char_to_page(file_cas_id)
        layout_summaries = _load_layout_summaries(file_cas_id)

        meta_payload = _build_meta_payload(
            source_meta=file_source_meta,
            source_url=source_url,
            added_at=file_added_at,
            mtime_ns=file_mtime_ns,
        )

        text_emb = get_text_embedder()
        sparse_emb = get_sparse_embedder()
        with _stage("embed_text.ensure_collection"):
            vector_store.ensure_chunks_collection(text_dim=text_emb.dim)

        if chunk_data:
            texts = [t for _, t, _, _, _, _ in chunk_data]
            # Embed in batches and emit per-batch progress — a big spreadsheet
            # (1000s of chunks) is otherwise a single multi-minute call that
            # shows zero movement. Each batch acquires gpu_lock independently,
            # which also lets search interleave between batches.
            with _stage("embed_text.dense", count=len(texts)):
                denses, sparses = [], []
                total = len(texts)
                for i in range(0, total, _EMBED_PROGRESS_BATCH):
                    batch = texts[i : i + _EMBED_PROGRESS_BATCH]
                    denses.extend(text_emb.embed_documents(batch))
                    sparses.extend(sparse_emb.embed_documents(batch))
                    _publish_job_progress(
                        "embed_text", min(i + _EMBED_PROGRESS_BATCH, total), total
                    )
            points: list = []
            for (cid, text, idx, nearby, c_start, c_end), dense, sparse in zip(
                chunk_data, denses, sparses, strict=True
            ):
                primary_page = (
                    primary_page_for_range(char_to_page, c_start or 0, c_end or 0)
                    if char_to_page
                    else None
                )
                pages = (
                    pages_for_range(
                        char_to_page, c_start or 0, c_end or (c_start or 0) + 1
                    )
                    if char_to_page
                    else []
                )
                summary = layout_summaries.get(primary_page) if primary_page else None
                points.append(
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
                        tab=tab,
                        dense_model_version=settings.dense_version,
                        sparse_model_version=settings.sparse_version,
                        allowed_users=allowed_users,
                        char_start=c_start,
                        char_end=c_end,
                        page=primary_page,
                        pages=pages,
                        layout_summary=summary,
                        meta_payload=meta_payload,
                    )
                )
        else:
            points = []

        with _stage("embed_text.upsert", count=len(points)):
            vector_store.replace_chunks_for_file(file_id, points)
        _decrement_pending_embeds(file_id, round_token)
        logger.info("embed_text done: points=%d", len(points))


def _embed_image_sync(image_id: int, round_token: int | None = None) -> None:
    from ...cas import store as cas_store
    from .. import vector_store
    from ..acl import allowed_user_ids_for_file
    from ..embedding import get_image_embedder

    with bind_context(image_id=image_id, round=round_token):
        logger.info("embed_image begin")
        settings = get_settings()
        with _stage("embed_image.load"), session_scope() as s:
            image = s.get(Image, image_id)
            if image is None:
                logger.info("embed_image abort: image gone")
                return
            file_id = image.file_id
            cas_id = image.image_cas_id
            anchor = image.anchor_chunk
            page = image.page
            file = s.get(File, file_id)
            if file is None:
                logger.info("embed_image abort: parent file gone")
                return
            if round_token is not None and file.embed_round != round_token:
                logger.info(
                    "embed_image abort: stale round (job=%s file=%s)",
                    round_token,
                    file.embed_round,
                )
                return
            folder_id = file.folder_id
            rel_path = file.rel_path
            file_cas_id = file.file_cas_id
            file_source_meta = file.source_meta
            file_source_url = file.source_url
            file_added_at = file.added_at
            file_mtime_ns = file.mtime_ns
            allowed_users = allowed_user_ids_for_file(s, file_id)

        layout_summaries = _load_layout_summaries(file_cas_id)
        layout_summary = layout_summaries.get(page) if page is not None else None
        meta_payload = _build_meta_payload(
            source_meta=file_source_meta,
            source_url=file_source_url,
            added_at=file_added_at,
            mtime_ns=file_mtime_ns,
        )

    with bind_context(image_id=image_id, file_id=file_id):
        image_emb = get_image_embedder()
        with _stage("embed_image.ensure_collection"):
            vector_store.ensure_images_collection(image_dim=image_emb.dim)

        with _stage("embed_image.find_existing"):
            existing = vector_store.find_image_point_by_cas(cas_id)
        if existing is not None:
            with _stage("embed_image.attach_existing"):
                vector_store.add_file_to_image_point(existing["id"], file_id)
            logger.info("embed_image dedup: reused cas=%s", cas_id)
        else:
            with _stage("embed_image.read_blob"):
                data = cas_store.read_image_blob(cas_id)
            with _stage("embed_image.encode", bytes=len(data)):
                vec = image_emb.embed_image(data)
            with _stage("embed_image.upsert"):
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
                        layout_summary=layout_summary,
                        meta_payload=meta_payload,
                    ),
                    file_ids=[file_id],
                )
            logger.info("embed_image done: cas=%s", cas_id)

        _decrement_pending_embeds(file_id, round_token)


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
