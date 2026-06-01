"""End-to-end ``extract`` job handler.

Reindex is whole-file: any change resets ``state='pending'`` and re-runs
the full extract → chunk → embed pipeline against the new bytes. Image
↔ chunk linkage is built here too: every extracted image carries an
anchor chunk (the chunk straddling its position in the markdown);
chunks within ``chunk_image_link_radius`` get a ``nearby_image`` link
with chunk-index distance as the score.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select, text

from ..cas import store as cas_store
from ..config import get_settings
from ..db.database import session_scope
from ..db.models import Chunk, ChunkImageLink, File, Folder, Image, Job
from ..logging_config import bind_context
from . import events, job_queue
from .chunking import ChunkInfo, anchor_chunk_for_position, get_default_chunking_registry
from .parsers.registry import get_default_registry

logger = logging.getLogger(__name__)

_ERROR_FIELD_MAX = 4000  # cap stored traceback so an error row stays scannable

# Process-wide lock around the extract pipeline.
#
# Originally added because PyMuPDF / cairo / some Pillow decoders are not
# fully thread-safe at the C level and N parallel extracts produced glibc
# heap corruption. The default worker pool is now size=1 (see
# settings.resolved_workers), so two workers cannot collide here even
# without the lock.
#
# The lock is still necessary because ``wipe_file_data`` runs from a REST
# handler thread (the /reindex endpoint) and must not race with the
# worker thread mid-extract: the worker reads + replaces image rows for
# a file, and a concurrent wipe in another thread would either delete
# stale rows by id (no-op) or leave the new rows in place (the bug
# users see as "reindex did nothing"). Holding _EXTRACT_LOCK across the
# wipe waits out any in-flight extract before deleting.
#
# gpu_lock (services/gpu_lock.py) is a separate, finer-grained lock that
# also serializes search query embeds against indexing — search runs on
# the asyncio loop, not the worker, so this is the only thing keeping
# query and indexing from racing on the GPU.
_EXTRACT_LOCK = threading.Lock()


@contextmanager
def _stage(name: str, **extra):
    """Log entry/exit + elapsed ms for a single indexing stage."""
    start = time.perf_counter()
    if extra:
        logger.debug("stage %s start %s", name, extra)
    else:
        logger.debug("stage %s start", name)
    try:
        yield
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception("stage %s failed after %.1fms", name, elapsed_ms)
        raise
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug("stage %s done in %.1fms", name, elapsed_ms)


def _format_exception(prefix: str) -> str:
    """Build the string we put into File.error — prefix + tail of traceback."""
    tb = traceback.format_exc()
    msg = f"{prefix}\n{tb}"
    if len(msg) > _ERROR_FIELD_MAX:
        msg = msg[: _ERROR_FIELD_MAX - 3] + "..."
    return msg


async def run_extract(payload: dict) -> None:
    file_id = int(payload["file_id"])
    await asyncio.to_thread(_run_extract_sync, file_id)


def _run_extract_sync(file_id: int) -> None:
    # Serialize the extract pipeline process-wide. See the comment on
    # _EXTRACT_LOCK for why — TL;DR: PyMuPDF + cairo are not thread-safe
    # at the C level and were producing heap corruption under N=24 workers.
    with bind_context(file_id=file_id):
        wait_started = time.perf_counter()
        with _EXTRACT_LOCK:
            wait_ms = (time.perf_counter() - wait_started) * 1000
            if wait_ms > 100:
                logger.debug("extract queue wait=%.0fms", wait_ms)
            try:
                _run_extract_inner(file_id)
            except Exception:
                _mark_error(file_id, _format_exception("extract crashed"))


def _run_extract_inner(file_id: int) -> None:
    extract_started = time.perf_counter()
    logger.info("extract begin")

    with _stage("resolve_path"):
        abs_path = _resolve_path(file_id)
    if abs_path is None:
        logger.info("extract abort: file or folder gone")
        return  # file or folder gone; ``delete_file`` cleans up

    if not abs_path.exists() or not abs_path.is_file():
        logger.info("extract abort: path missing on disk path=%s", abs_path)
        _mark_state(file_id, state="deleted")
        return

    logger.debug("path=%s", abs_path)
    settings = get_settings()
    try:
        with _stage("stat"):
            stat = abs_path.stat()
    except OSError as e:
        _mark_error(file_id, f"stat failed: {e}")
        return
    from .indexing_caps import DATA_EXTENSIONS, get_caps

    caps = get_caps()
    if stat.st_size > caps.max_file_bytes:
        logger.warning(
            "size %d exceeds limit %d", stat.st_size, caps.max_file_bytes
        )
        _mark_state(
            file_id,
            state="unsupported",
            error=f"size {stat.st_size} exceeds max_file_bytes {caps.max_file_bytes}",
        )
        return

    # Data-file extension cap: a 142 MB JSON produced 81 719 chunks in the
    # wild and dominated the index without being useful as RAG content. We
    # park oversized data files in ``unsupported`` so they show up on the
    # by-extension sidebar with a clear reason instead of being silently
    # chunk-bombs. ``data_file_max_bytes = 0`` disables the special case.
    ext = abs_path.suffix.lower()
    if (
        caps.data_file_max_bytes > 0
        and ext in DATA_EXTENSIONS
        and stat.st_size > caps.data_file_max_bytes
    ):
        logger.info(
            "data-file %s size %d exceeds data_file_max_bytes %d — marking unsupported",
            ext, stat.st_size, caps.data_file_max_bytes,
        )
        _mark_state(
            file_id,
            state="unsupported",
            error=(
                f"{ext} size {stat.st_size} exceeds data_file_max_bytes "
                f"{caps.data_file_max_bytes}"
            ),
        )
        return

    try:
        with _stage("read_bytes", size=stat.st_size):
            raw = abs_path.read_bytes()
    except OSError as e:
        _mark_error(file_id, f"read failed: {e}")
        return

    with _stage("sha256"):
        new_sha = hashlib.sha256(raw).hexdigest()
    logger.debug("sha=%s size=%d", new_sha, len(raw))

    from .meta_sidecar import load as load_meta_sidecar

    meta = load_meta_sidecar(abs_path)
    effective_mtime_ns = (
        meta.modified_at_ns if (meta and meta.modified_at_ns is not None)
        else stat.st_mtime_ns
    )

    if _short_circuit_unchanged(file_id, new_sha, effective_mtime_ns):
        logger.info("extract skip: unchanged sha")
        return

    with _stage("find_parser"):
        parser = get_default_registry().find(abs_path)
    if parser is None:
        # Not an indexer failure — just a file type we don't have a parser for
        # (e.g. .svg, .mp4, binary blobs). Record it as ``unsupported`` so it
        # doesn't pollute the error counters; reindex will retry if we add a
        # parser for that extension later.
        ext = abs_path.suffix or "(no ext)"
        logger.info("no parser for %s — marking unsupported", ext)
        _mark_state(file_id, state="unsupported", error=f"no parser for {ext}")
        return
    logger.info("parser=%s", parser.__class__.__name__)

    try:
        with _stage("parse"):
            result = parser.parse(abs_path)
    except Exception:
        _mark_error(file_id, _format_exception(f"{parser.__class__.__name__}.parse raised"))
        return
    if not result.success:
        logger.warning("parser reported failure: %s", result.error)
        _mark_error(file_id, result.error or "parse failed")
        return
    logger.info(
        "parsed: chars=%d images=%d", len(result.content), len(result.images)
    )

    try:
        with _stage("cas_write_text"):
            cas_store.write_file_blob(new_sha, "text.md", result.content)
        with _stage("cas_write_images", count=len(result.images)):
            image_shas: list[str] = [
                cas_store.write_image_blob(img.bytes) for img in result.images
            ]
        with _stage("cas_write_page_images", count=len(result.page_images)):
            page_image_shas: list[str] = [
                cas_store.write_image_blob(p.bytes) for p in result.page_images
            ]
        with _stage("chunk", path=str(abs_path)):
            chunks = get_default_chunking_registry().chunk(result.content, abs_path)
        logger.info("chunked: count=%d", len(chunks))

        # Persist the structured layout next to text.md so it dedupes
        # via the same content hash. Empty for parsers that don't emit
        # layout — keep the file off disk in that case so callers can
        # treat its absence as "no layout".
        layout_summaries: dict[int, dict] = {}
        if result.page_layout:
            from .layout import summaries_by_page

            with _stage("cas_write_page_layout", count=len(result.page_layout)):
                cas_store.write_file_blob(
                    new_sha,
                    "page_layout.json",
                    json.dumps(result.page_layout),
                )
            with _stage("compute_layout_summaries"):
                layout_summaries = summaries_by_page(result.page_layout)
            with _stage("cas_write_layout_summaries", count=len(layout_summaries)):
                # JSON object keys must be strings; the consumer parses
                # them back to int via _load_layout_summaries.
                cas_store.write_file_blob(
                    new_sha,
                    "layout_summaries.json",
                    json.dumps({str(p): s for p, s in layout_summaries.items()}),
                )
        if result.char_to_page:
            with _stage("cas_write_char_to_page", count=len(result.char_to_page)):
                cas_store.write_file_blob(
                    new_sha,
                    "char_to_page.json",
                    json.dumps(result.char_to_page),
                )

        # On-demand asset menu (e.g., CAD per-component projections,
        # xlsx chart renderers — anything the LLM can call via
        # ``request_asset``). Parsers that don't expose any leave
        # ``on_demand_assets`` empty and we skip the write so absence
        # naturally means "no menu".
        if result.on_demand_assets:
            with _stage("cas_write_on_demand_assets", count=len(result.on_demand_assets)):
                cas_store.write_file_blob(
                    new_sha,
                    "on_demand_assets.json",
                    json.dumps([a.as_dict() for a in result.on_demand_assets]),
                )

        with _stage("cas_write_manifest"):
            cas_store.write_file_blob(
                new_sha,
                "manifest.json",
                json.dumps(
                    {
                        "parser": parser.__class__.__name__,
                        "chunk_count": len(chunks),
                        "image_count": len(result.images),
                        "page_image_count": len(result.page_images),
                        "page_layout_block_count": len(result.page_layout),
                        "char_to_page_anchors": len(result.char_to_page),
                        "layout_summary_pages": len(layout_summaries),
                        "image_positions": [img.position for img in result.images],
                        "metadata": result.metadata,
                    },
                    indent=2,
                ),
            )

        with _stage("commit_indexing"):
            commit = _commit_indexing(
                file_id=file_id,
                new_sha=new_sha,
                mtime_ns=effective_mtime_ns,
                chunks=chunks,
                images=list(zip(image_shas, result.images, strict=True)),
                page_images=list(
                    zip(page_image_shas, result.page_images, strict=True)
                ),
                nearby_radius=settings.nearby_radius,
            )
    except Exception:
        _mark_error(file_id, _format_exception("post-parse pipeline failed"))
        return

    if commit is None:
        # File row vanished mid-commit — nothing to embed.
        return
    round_token, image_ids = commit

    # Run embeds inline. _EXTRACT_LOCK is still held, so this guarantees the
    # file goes pending → indexed within a single extract job — no other
    # file can sneak past us into the "extracted" / "embedding" buckets
    # while we're embedding. Each individual embed still acquires gpu_lock
    # for the actual GPU forward pass; everything else (DB writes, Qdrant
    # upserts) is plain sequential I/O.
    try:
        if chunks:
            with _stage("embed_text_inline"):
                _embed_text_sync(file_id, round_token=round_token)
    except Exception:
        _mark_error(file_id, _format_exception("inline text embed failed"))
        return
    # Per-image embed failures are *not* fatal to the file. One
    # unparseable WMF / EMF / corrupt PNG buried in an old PowerPoint
    # shouldn't tank the whole document — we'd be hiding hundreds of
    # other useful pages of text for one bad clipart. Each image
    # records its own error inside ``_embed_image_sync``; we just log
    # at the file level and let the rest of the file complete.
    image_failures: list[tuple[int, BaseException]] = []
    for image_id in image_ids:
        try:
            with _stage("embed_image_inline", image_id=image_id):
                _embed_image_sync(image_id, round_token=round_token)
        except Exception as e:  # noqa: BLE001
            image_failures.append((image_id, e))
            logger.warning(
                "embed_image failed for image_id=%s in file_id=%s: %s",
                image_id, file_id, e,
            )
    if image_failures:
        logger.warning(
            "file_id=%s indexed with %d/%d image embed failures "
            "(text + remaining images still indexed)",
            file_id, len(image_failures), len(image_ids),
        )

    elapsed_ms = (time.perf_counter() - extract_started) * 1000
    logger.info(
        "extract done: chunks=%d images=%d in %.1fms",
        len(chunks),
        len(result.images),
        elapsed_ms,
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
    healed = False
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None or file.file_cas_id != new_sha:
            return False
        file.mtime_ns = mtime_ns
        file.last_seen_at = int(time.time())
        # Heal stale non-terminal states. If a previous extract was orphaned
        # (uvicorn --reload, OOM) the file row may sit in `pending`/`extracted`/
        # `embedding` even though the CAS sha matches what's already on disk
        # *and* on Qdrant. Without this, the file ends up stuck: extract is
        # re-enqueued, hits this skip, returns — and the queue empties with
        # the row still non-terminal, so the UI shows "indexing" forever.
        #
        # If pending_embeds > 0 we don't short-circuit at all: embed work was
        # lost mid-flight, so let the caller fall through to a full re-extract
        # which rewrites CAS/chunks/images and re-enqueues embeds.
        if file.state in ("pending", "extracted", "embedding"):
            if file.pending_embeds > 0:
                return False
            file.state = "indexed"
            file.error = None
            healed = True
    if healed:
        publish_file_upserted(file_id)
    return True


def _mark_state(file_id: int, *, state: str, error: str | None = None) -> None:
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        file.state = state
        file.error = error
    publish_file_upserted(file_id)


def publish_file_upserted(file_id: int) -> None:
    """Publish ``file.upserted`` + ``folder.stats_changed`` for the SPA.

    Single source of truth — every call site that mutates a ``File`` row
    must call this (or ``events.publish('files', {'type': 'file.deleted',
    ...})`` for terminal removals). Reads the row back from the DB inside
    a fresh session so the payload reflects the committed state, not
    whatever the caller happens to have in memory. No-op if the row
    vanished between the mutation and this call.

    The same hook publishes ``folder.stats_changed`` so the sidebar's
    chunk/image/byte counters stay in lockstep with the files store.
    Both events go out in the same session — they reflect the same
    committed snapshot. ``events.py`` coalesces the snapshots per
    folder_id, so a 200-file extract burst produces one delivered
    stats event per folder, not 200.
    """
    from .folder_stats import publish_folder_stats

    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        events.publish(
            "files",
            {
                "type": "file.upserted",
                "file": file_event_payload(file),
            },
        )
        publish_folder_stats(s, file.folder_id)


def file_event_payload(file: File, *, image_count: int | None = None) -> dict:
    """Shape the SPA expects on every ``file.upserted`` event.

    ``image_count`` lets the file tree decide expandability up front — a docx /
    xlsx with no extracted images shows no expand chevron at all (rather than
    expanding to an empty "No previews"). Pass it precomputed (the snapshot
    builder counts in bulk to avoid N+1); otherwise it's counted inline from the
    file's own session.
    """
    if image_count is None:
        from sqlalchemy import func
        from sqlalchemy.orm import object_session

        sess = object_session(file)
        image_count = (
            sess.execute(
                select(func.count()).select_from(Image).where(Image.file_id == file.id)
            ).scalar_one()
            if sess is not None
            else 0
        )
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
        "tab": file.tab,
        "image_count": image_count,
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
    images: list[tuple[str, object]],  # (sha, ExtractedImage) — kind='figure'
    page_images: list[tuple[str, object]],  # (sha, RenderedPage) — kind='page_render'
    nearby_radius: int,
) -> tuple[int, list[int]] | None:
    """Commit chunks/images for ``file_id`` and return ``(round_token, figure_image_ids)``.

    ``figure_image_ids`` covers only ``kind='figure'`` rows; page renders
    are intentionally excluded because we do not embed them (they exist
    purely for layout retrieval) and the caller's inline embed loop must
    therefore skip them.

    Embeds are NOT enqueued here — the caller in ``_run_extract_inner`` runs
    them inline so the entire pipeline (extract + embeds) runs end-to-end
    within a single extract job, holding ``_EXTRACT_LOCK``. That makes the
    whole pipeline file-by-file: there is at most one file in
    ``state='extracted'`` (or ``'embedding'``) at any time across the
    process. Previously we fanned the embeds out to other workers, which
    produced "in progress: N" buildup in the UI.

    Returns ``None`` if the file was deleted between scan and commit.
    """
    now = int(time.time())
    image_ids: list[int] = []
    new_round: int | None = None
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return None

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
                kind="figure",
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

        # Page renders sit at image_index >= len(figures) so the (file_id,
        # image_index) UNIQUE constraint stays satisfied alongside figures.
        # Intentionally no anchor_chunk + no chunk_image_links: these are
        # full-page rasters, not crops; linking every chunk on a page would
        # bury figure links in noise.
        for j, (sha, page_img) in enumerate(page_images):
            page_row = Image(
                file_id=file_id,
                image_index=len(images) + j,
                image_cas_id=sha,
                anchor_chunk=None,
                page=page_img.page,
                width=page_img.width,
                height=page_img.height,
                mime=page_img.mime,
                kind="page_render",
                created_at=now,
            )
            s.add(page_row)
            cas_store.incref(s, cas_store.KIND_IMAGE, sha)
        s.flush()

        # Bump embed_round so any in-flight decrements from a prior round are
        # ignored (see ``_decrement_pending_embeds``). Without this guard, a
        # stale embed completing after re-extract would corrupt the new
        # ``pending_embeds`` counter and leave the file permanently stuck.
        new_round = (file.embed_round or 0) + 1
        file.embed_round = new_round
        file.file_cas_id = new_sha
        file.mtime_ns = mtime_ns
        file.last_seen_at = now
        file.last_indexed_at = now
        # page_render rows are deliberately excluded from pending_embeds:
        # we don't run SigLIP on them, so there is no embed to wait on.
        file.pending_embeds = (1 if chunks else 0) + len(images)
        if file.pending_embeds == 0:
            # Empty file (no chunks, no images — typical of zero-byte
            # __init__.py and similar marker files). Without this snap,
            # the file sits in 'extracted' forever because the
            # 'extracted → indexed' transition only happens inside
            # _decrement_pending_embeds, which never runs when no embed
            # jobs are enqueued.
            file.state = "indexed"
        else:
            file.state = "extracted"
        file.error = None
        _committed_file_id = file.id

        image_ids = [
            iid
            for (iid,) in s.execute(
                select(Image.id)
                .where(Image.file_id == file_id, Image.kind == "figure")
                .order_by(Image.image_index)
            ).all()
        ]

    # Scrub orphan Qdrant image points from the prior extract of this
    # file. Image DB rows were just deleted, but their Qdrant points
    # (keyed by the now-stale image_id) were not — and the embed step's
    # CAS-dedup path would re-attach our file_id to that orphan instead
    # of writing a fresh point at the new image_id. Net symptom: search
    # returns image_ids that don't exist in the DB anymore.
    #
    # ``remove_file_from_image_points`` strips this file_id from every
    # image point's ``file_ids``; points that empty get deleted. After
    # this, ``_embed_image_sync`` either finds a still-shared point
    # (cross-file CAS reuse) and attaches us correctly, or finds none
    # and creates a fresh point at the new image_id.
    #
    # Chunks don't need this — ``replace_chunks_for_file`` is already
    # an atomic delete-by-file_id + upsert.
    from . import vector_store

    vector_store.remove_file_from_image_points(file_id)

    publish_file_upserted(_committed_file_id)
    return new_round, image_ids


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


async def run_sync(payload: dict) -> None:
    """Worker handler for ``sync`` jobs: pulls a folder's remote, mirrors files
    onto disk, and records status on the ``folder_sync_sources`` row.

    Side effects on disk drive the existing extract / delete pipeline through
    the watcher — this handler does not enqueue extracts itself.
    """
    folder_id = int(payload["folder_id"])
    with bind_context(folder_id=folder_id):
        try:
            await _run_sync_inner(folder_id)
        except Exception:
            logger.exception("sync failed")
            _mark_sync_error(folder_id, _format_exception("sync failed"))
            raise


def _publish_sync_source_changed(folder_id: int) -> None:
    """Push the current sync-source status to subscribed clients.

    The sidebar's badge already follows ``folder.sync_progress`` for the
    in-progress phases. This event covers the *terminal* fields the modal
    renders — ``sync_status`` / ``sync_error`` / ``last_synced_at`` — so an
    open modal updates the moment a job finishes (or errors, or starts in
    another tab), without needing the user to close + reopen it.
    """
    with session_scope() as s:
        from ..db.models import FolderSyncSource

        src = s.get(FolderSyncSource, folder_id)
        if src is None:
            return
        event = {
            "type": "folder.sync_source_changed",
            "folder_id": folder_id,
            "sync_status": src.sync_status,
            "sync_error": src.sync_error,
            "last_synced_at": src.last_synced_at,
        }
    events.publish("folders", event)


async def _run_sync_inner(folder_id: int) -> None:
    from .sync import get_connector

    with session_scope() as s:
        folder = s.get(Folder, folder_id)
        if folder is None:
            logger.info("sync abort: folder %d gone", folder_id)
            return
        from ..db.models import FolderSyncSource

        source = s.get(FolderSyncSource, folder_id)
        if source is None:
            raise RuntimeError(f"no sync source configured for folder {folder_id}")
        folder_path = folder.path
        source_type = source.source_type

        # Each connector builds its own kwargs from the row (the per-type
        # switchboard used to live here). Done while the session is open since
        # it reads ``source`` fields; we then release the session before the
        # slow network/disk work below.
        connector = get_connector(source_type)
        cfg: dict[str, object] = connector.resolve_config(source)

        source.sync_status = "syncing"
        source.sync_error = None

    _publish_sync_source_changed(folder_id)
    logger.info("sync begin folder=%s type=%s", folder_path, source_type)

    # Bridge the connector's progress callback into the WS event stream so
    # the SPA can show a live "Syncing — listing 1/3" / "Syncing — 47/200"
    # pill. Sync runs on a worker thread (via ``asyncio.to_thread`` inside
    # ``GoogleDriveConnector.sync``) but ``events.publish`` is thread-safe
    # — it round-trips through ``run_coroutine_threadsafe`` if the asyncio
    # loop has been wired in.
    def _on_progress(
        phase: str,
        done: int,
        total: int,
        detail: dict | None = None,
    ) -> None:
        event = {
            "type": "folder.sync_progress",
            "folder_id": folder_id,
            "phase": phase,
            "done": done,
            "total": total,
        }
        if detail:
            event["detail"] = detail
        events.publish("folders", event)

    if connector.supports_progress:
        cfg["progress_cb"] = _on_progress

    # Initial event from the worker side, before the connector starts —
    # gives the SPA something to render the moment the sync job runs,
    # rather than waiting for the connector to issue its first progress
    # call (which can be seconds later if Drive auth is slow).
    _on_progress("queued", 0, 0)

    started = time.perf_counter()
    try:
        stats = await connector.sync(
            folder_root=Path(folder_path),
            **cfg,
        )
    except Exception:
        with session_scope() as s2:
            from ..db.models import FolderSyncSource

            src = s2.get(FolderSyncSource, folder_id)
            if src is not None:
                src.sync_status = "error"
        _publish_sync_source_changed(folder_id)
        # Final event so the SPA's badge clears even on failure.
        _on_progress("done", 0, 0, None)
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "sync done folder=%s in %.1fs: %s", folder_path, elapsed, stats.as_dict()
    )

    with session_scope() as s3:
        from ..db.models import FolderSyncSource

        src = s3.get(FolderSyncSource, folder_id)
        if src is not None:
            src.sync_status = "error" if stats.errors else "idle"
            src.sync_error = "; ".join(stats.errors) if stats.errors else None
            src.last_synced_at = int(time.time())
            # Microsoft rotates refresh tokens on most refresh calls; the
            # connector parks the rotated value on its MicrosoftAuth and
            # we persist it here so the next sync can mint a token.
            if source_type in ("sharepoint", "teams"):
                ms_auth = cfg.get("auth")
                rotated = getattr(ms_auth, "rotated_refresh_token", None)
                if rotated:
                    src.ms_refresh_token = rotated

    _publish_sync_source_changed(folder_id)
    # Final clear-the-badge event. The GD connector already emits "done"
    # on success, but other connectors may not — this guarantees the SPA
    # drops the sync pill no matter which connector ran.
    _on_progress("done", 0, 0, None)


def _mark_sync_error(folder_id: int, message: str) -> None:
    with session_scope() as s:
        from ..db.models import FolderSyncSource

        src = s.get(FolderSyncSource, folder_id)
        if src is None:
            return
        src.sync_status = "error"
        src.sync_error = message[:4000]
    _publish_sync_source_changed(folder_id)


async def run_delete_file(payload: dict) -> None:
    file_id = int(payload["file_id"])
    await asyncio.to_thread(_delete_file_sync, file_id)


async def run_reindex_folder(payload: dict) -> None:
    """Worker handler: wipe + reset + re-enqueue every matched file.

    The REST endpoint enqueues this and returns immediately so the request
    thread isn't blocked behind ``_EXTRACT_LOCK`` waiting for the current
    extract to finish. Once this handler runs, *we* are the extract worker
    — no other extract can be in flight, so wipes are uncontended and the
    state machine transitions cleanly.
    """
    file_ids: list[int] = list(payload.get("file_ids") or [])
    folder_id = payload.get("folder_id")
    if not file_ids:
        return
    with bind_context(folder_id=folder_id):
        logger.info("reindex begin: %d file(s)", len(file_ids))
        await asyncio.to_thread(_run_reindex_sync, folder_id, file_ids)
        logger.info("reindex done: %d file(s)", len(file_ids))


# Wipe progress is broadcast via WebSocket so the SPA can show a live
# "Wiping… 600/1969" pill on the folder card. Batched in chunks of 200 so
# we publish on the order of 10 events for a 2k-file folder, not one per
# file.
_REINDEX_PROGRESS_CHUNK = 200


def _publish_reindex_progress(
    folder_id: int | None,
    *,
    phase: str,
    done: int,
    total: int,
) -> None:
    if folder_id is None:
        return
    events.publish(
        "folders",
        {
            "type": "folder.reindex_progress",
            "folder_id": folder_id,
            "phase": phase,  # "cancelling" | "wiping" | "queueing" | "done"
            "done": done,
            "total": total,
        },
    )


def _run_reindex_sync(folder_id: int | None, file_ids: list[int]) -> None:
    """Synchronous body of run_reindex_folder. Runs in the worker thread.

    Performs three batched phases (each emits a progress event):

    1. **cancelling** — flip queued extract jobs to ``done(superseded)``
       so a pre-reindex enqueue can't fire on a half-wiped file.
    2. **wiping** — bulk-delete every chunk + image row + Qdrant point
       across the target file_ids in just a few round-trips, regardless
       of how many files are involved.
    3. **queueing** — flip every file row back to ``pending`` and enqueue
       a fresh extract.

    Old code did all three per-file in a Python loop (1969 iterations →
    6 minutes for the user's git-test folder). This version uses bulk SQL
    DELETE + folder-scope Qdrant deletes, taking each phase from O(N)
    round-trips down to O(1).
    """
    from sqlalchemy import delete as sa_delete

    from ..cas import store as cas_store
    from . import vector_store

    total = len(file_ids)

    # ---- Phase 1: cancelling stale extracts ---------------------------------
    cancelled = 0
    with session_scope() as s:
        # One query per ~200 file_ids — SQLite has a default 999-parameter
        # limit, so chunked IN is the safest portable shape.
        for chunk in _chunked(file_ids, _REINDEX_PROGRESS_CHUNK):
            q = s.execute(
                select(Job).where(
                    Job.state == "queued",
                    Job.kind == "extract",
                    Job.dedup_key.in_([f"extract:{fid}" for fid in chunk]),
                )
            ).scalars()
            for j in q:
                j.state = "done"
                j.error = "superseded by reindex"
                j.finished_at = int(time.time())
                cancelled += 1
    if cancelled:
        logger.info("reindex: cancelled %d stale extract job(s)", cancelled)
    _publish_reindex_progress(folder_id, phase="cancelling", done=total, total=total)

    # ---- Phase 2: wiping (the slow phase, now bulk) -------------------------
    _publish_reindex_progress(folder_id, phase="wiping", done=0, total=total)

    # 2a. CAS decrefs need per-row context (each chunk + image references
    # one SHA), so we still touch every row — but in batches with one
    # transaction per batch, not one per file. The decref is a SQLite-only
    # bookkeeping update so it's cheap.
    with _EXTRACT_LOCK:
        wiped = 0
        for chunk in _chunked(file_ids, _REINDEX_PROGRESS_CHUNK):
            with session_scope() as s:
                # Decref CAS for every image whose file is being wiped.
                rows = s.execute(
                    select(Image.image_cas_id, Image.file_id).where(
                        Image.file_id.in_(chunk)
                    )
                ).all()
                for sha, _fid in rows:
                    cas_store.decref(s, cas_store.KIND_IMAGE, sha)
                # And for the file blob itself.
                file_shas = s.execute(
                    select(File.file_cas_id).where(
                        File.id.in_(chunk),
                        File.file_cas_id.is_not(None),
                    )
                ).all()
                for (sha,) in file_shas:
                    cas_store.decref(s, cas_store.KIND_FILE, sha)
                # Bulk row deletes. ChunkImageLink is CASCADEd from both
                # parents, so this is the only DELETE needed for it too.
                s.execute(sa_delete(Image).where(Image.file_id.in_(chunk)))
                s.execute(sa_delete(Chunk).where(Chunk.file_id.in_(chunk)))
            wiped += len(chunk)
            _publish_reindex_progress(
                folder_id, phase="wiping", done=wiped, total=total
            )

        # 2b. Wipe Qdrant chunk points for exactly the files being
        # re-extracted — scoped to ``file_ids``, NOT the whole folder. A
        # folder-wide delete here would nuke every other file's vectors on a
        # *partial* reindex (e.g. reindexing one file), leaving them indexed
        # in SQLite but absent from Qdrant. Batched filter-deletes keep a
        # full-folder reindex to a handful of round-trips.
        vector_store.delete_chunks_for_files(file_ids)
        deleted_image_points = vector_store.remove_files_from_image_points(file_ids)
        if deleted_image_points:
            logger.info(
                "reindex: removed %d image point(s) from Qdrant",
                deleted_image_points,
            )

    # ---- Phase 3: queueing fresh extracts -----------------------------------
    _publish_reindex_progress(folder_id, phase="queueing", done=0, total=total)
    upserts: list[int] = []
    queued = 0
    for chunk in _chunked(file_ids, _REINDEX_PROGRESS_CHUNK):
        with session_scope() as s:
            files = s.execute(select(File).where(File.id.in_(chunk))).scalars().all()
            for f in files:
                f.file_cas_id = None
                f.state = "pending"
                f.error = None
                f.pending_embeds = 0
                job_queue.enqueue(
                    s, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
                )
                upserts.append(f.id)
        queued += len(chunk)
        _publish_reindex_progress(
            folder_id, phase="queueing", done=queued, total=total
        )

    for fid in upserts:
        publish_file_upserted(fid)
    _publish_reindex_progress(folder_id, phase="done", done=total, total=total)


def _chunked(seq: list[int], size: int):
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _load_char_to_page(file_cas_id: str | None) -> list[tuple[int, int]]:
    """Pull the parser's char→page anchors back from CAS (or ``[]``)."""
    if not file_cas_id:
        return []
    try:
        raw = cas_store.read_file_blob(file_cas_id, "char_to_page.json")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("char_to_page.json unparseable for cas=%s", file_cas_id)
        return []
    out: list[tuple[int, int]] = []
    for entry in data if isinstance(data, list) else []:
        if (
            isinstance(entry, list | tuple)
            and len(entry) == 2
            and isinstance(entry[0], int)
            and isinstance(entry[1], int)
        ):
            out.append((entry[0], entry[1]))
    return out


def _load_layout_summaries(file_cas_id: str | None) -> dict[int, dict]:
    """Pull per-page layout summaries back from CAS (or ``{}``).

    Stored as ``{"<page_int_str>": {layout_*: ...}}``; we re-parse the
    keys to ``int``. Pages without an entry just won't get layout
    fields attached to their chunks/images, which is the desired
    behaviour (consumer treats missing as "unknown layout").
    """
    if not file_cas_id:
        return {}
    try:
        raw = cas_store.read_file_blob(file_cas_id, "layout_summaries.json")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("layout_summaries.json unparseable for cas=%s", file_cas_id)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, dict] = {}
    for k, v in data.items():
        try:
            page = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            out[page] = v
    return out


def _embed_text_sync(file_id: int, round_token: int | None = None) -> None:
    from . import vector_store
    from .acl import allowed_user_ids_for_file
    from .embedding import get_sparse_embedder, get_text_embedder
    from .layout import pages_for_range, primary_page_for_range

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
            allowed_users = allowed_user_ids_for_file(s, file_id)
            folder = s.get(Folder, folder_id)
            folder_path = folder.path if folder else None

        char_to_page = _load_char_to_page(file_cas_id)
        layout_summaries = _load_layout_summaries(file_cas_id)

        from .meta_sidecar import load as load_meta_sidecar

        meta_payload: dict | None = None
        if folder_path:
            abs_file = Path(folder_path) / rel_path
            meta = load_meta_sidecar(abs_file)
            if meta:
                meta_payload = meta.payload_fields or None

        text_emb = get_text_embedder()
        sparse_emb = get_sparse_embedder()
        with _stage("embed_text.ensure_collection"):
            vector_store.ensure_chunks_collection(text_dim=text_emb.dim)

        if chunk_data:
            texts = [t for _, t, _, _, _, _ in chunk_data]
            with _stage("embed_text.dense", count=len(texts)):
                denses = text_emb.embed_documents(texts)
            with _stage("embed_text.sparse", count=len(texts)):
                sparses = sparse_emb.embed_documents(texts)
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
    from ..cas import store as cas_store
    from . import vector_store
    from .acl import allowed_user_ids_for_file
    from .embedding import get_image_embedder

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
            allowed_users = allowed_user_ids_for_file(s, file_id)

        layout_summaries = _load_layout_summaries(file_cas_id)
        layout_summary = layout_summaries.get(page) if page is not None else None

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
                    ),
                    file_ids=[file_id],
                )
            logger.info("embed_image done: cas=%s", cas_id)

        _decrement_pending_embeds(file_id, round_token)


def wipe_file_data(file_id: int) -> None:
    """Delete every artifact associated with ``file_id`` *except* the file row.

    Wipes chunks, images, ChunkImageLinks, CAS refcounts, Qdrant chunk points,
    and removes the file_id from any shared image points (deleting the image
    point entirely if no other file references it).

    Acquires ``_EXTRACT_LOCK`` for the duration so we never race with an
    in-flight extract on the same file. Without that, an extract committing
    its own image-replacement mid-wipe leaves us deleting stale rows by id
    (the new rows extract just wrote stay alive — chunk/image counts in the
    UI never drop). Symptom was a wave of SAWarning "expected to delete N
    row(s); 0 were matched" alongside a reindex that visibly did nothing.

    Used by:
    * ``_delete_file_sync`` — file is gone; we then delete the file row.
    * ``reindex_folder`` — caller wants the file row to stay (state will be
      reset to ``pending``) but every downstream artifact must vanish so the
      stats counts reflect reality and stale Qdrant points don't leak into
      search results during the re-extract window.
    """
    from sqlalchemy import delete as sa_delete

    from ..cas import store as cas_store
    from . import vector_store

    with _EXTRACT_LOCK, session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        old_sha = file.file_cas_id
        # Use bulk DELETE statements rather than ORM s.delete() per row.
        # Bulk statements run under the writer lock and operate on the
        # current committed snapshot, so we can't end up with stale ORM
        # rows whose ids no longer exist in the DB. Also faster.

        # Decref CAS for each image before we drop the rows. Reading the
        # SHAs in one shot keeps the CAS bookkeeping consistent without
        # holding a list of ORM instances around the bulk DELETE.
        old_image_shas = [
            sha
            for (sha,) in s.execute(
                select(Image.image_cas_id).where(Image.file_id == file_id)
            ).all()
        ]
        for sha in old_image_shas:
            cas_store.decref(s, cas_store.KIND_IMAGE, sha)
        if old_sha is not None:
            cas_store.decref(s, cas_store.KIND_FILE, old_sha)

        # ChunkImageLink has CASCADE on both sides; deleting parents
        # removes the link rows automatically.
        s.execute(sa_delete(Image).where(Image.file_id == file_id))
        s.execute(sa_delete(Chunk).where(Chunk.file_id == file_id))

    vector_store.delete_chunks_for_file(file_id)
    vector_store.remove_file_from_image_points(file_id)


def _delete_file_sync(file_id: int) -> None:
    """Worker handler: delete the file row plus every artifact under it."""
    from .folder_stats import publish_folder_stats

    # Capture folder_id before deleting so the post-delete stats publish
    # can find the folder row.
    folder_id: int | None = None
    wipe_file_data(file_id)
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is not None:
            folder_id = file.folder_id
            s.delete(file)
    events.publish(
        "files",
        {"type": "file.deleted", "file_id": file_id, "folder_id": folder_id},
    )
    if folder_id is not None:
        with session_scope() as s:
            publish_folder_stats(s, folder_id)


def _decrement_pending_embeds(file_id: int, round_token: int | None = None) -> None:
    """Decrement ``pending_embeds`` from a finishing embed job.

    The decrement is performed via an atomic SQL ``UPDATE`` so concurrent
    callers cannot lose updates — a read-modify-write through the ORM is racy
    when many embed jobs finish in the same SQLite WAL window (which happens
    constantly: image embeds that hit the dedup path complete in ~1 ms each).

    The same UPDATE doubles as the round-token guard: when a re-extract has
    already bumped ``embed_round``, ``rowcount`` comes back as 0 and we skip
    the state-transition check entirely.

    On reaching zero, transitions the file to ``indexed`` and clears any
    previously-recorded ``error`` (a successful embed cycle supersedes a
    transient failure that has since been retried).
    """
    params: dict[str, object] = {"id": file_id}
    guard = ""
    if round_token is not None:
        guard = " AND embed_round = :r"
        params["r"] = round_token
    state_changed = False
    with session_scope() as s:
        res = s.execute(
            text(
                "UPDATE files SET pending_embeds = MAX(0, pending_embeds - 1) "
                "WHERE id = :id" + guard
            ),
            params,
        )
        if res.rowcount == 0:
            logger.debug(
                "skip pending decrement: file gone or stale round (job=%s)",
                round_token,
            )
            return
        row = s.execute(
            text("SELECT pending_embeds, state FROM files WHERE id = :id"),
            {"id": file_id},
        ).first()
        if row is None:
            return
        pending, state = row
        if pending == 0 and state in ("extracted", "embedding", "error"):
            s.execute(
                text(
                    "UPDATE files SET state = 'indexed', error = NULL "
                    "WHERE id = :id"
                ),
                {"id": file_id},
            )
            state_changed = True
    # Only emit when the file's user-visible state actually changed.
    # Mid-run pending_embeds decrements (e.g. 17 -> 16) are noise the UI
    # neither displays nor needs — and at 24 workers each PDF can produce
    # 50+ such decrements per file. Coalescing in events.py would already
    # squash them, but skipping the publish entirely is cheaper.
    if state_changed:
        publish_file_upserted(file_id)


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


def reconcile_abandoned_extracts() -> int:
    """Reset files left in mid-pipeline state when their extract job died.

    A worker that crashed during ``_run_extract_inner`` (uvicorn --reload,
    OOM, ctrl-C) leaves the file row at ``state='extracted'`` or
    ``state='embedding'`` with a previous round's ``pending_embeds`` value
    that no longer matches any in-flight job — and ``_commit_indexing``
    already ran (or never reached the embed enqueue), so neither the file
    nor the queue knows that work is unfinished.

    For every such row that has *no* live extract/embed job, we kick it
    back to ``state='pending'``: the watcher's previously-emitted event for
    this file is gone, but ``reclaim_abandoned_jobs`` ran first and may
    have requeued the original extract; failing that, the next folder
    scan will pick it up.

    Files where ``pending_embeds=0`` and a CAS row exists take a fast path
    below: snap straight to ``indexed`` instead of cycling through a
    no-op re-extract.

    We also re-enqueue *pending* files that have no live extract job.
    Without this, a cancel-all from a previous run leaves files sitting
    in ``state='pending'`` forever: their dedup'd extract is in 'done'
    state so a new one would be admitted, but nothing actually calls
    enqueue() because the scanner only re-enqueues on mtime/size change.
    """
    repaired_ids: list[int] = []
    with session_scope() as s:
        candidates = list(
            s.execute(
                select(File).where(
                    File.state.in_(("extracted", "embedding")),
                )
            ).scalars()
        )
        # Build set of file_ids referenced by any live (queued/running)
        # extract or embed job — those don't need our help.
        live_files: set[int] = set()
        rows = s.execute(
            select(Job).where(
                Job.state.in_(("queued", "running")),
                Job.kind.in_(("extract", "embed_text", "embed_image")),
            )
        ).scalars()
        for j in rows:
            try:
                payload = json.loads(j.payload)
            except (TypeError, ValueError):
                continue
            fid = payload.get("file_id")
            if isinstance(fid, int):
                live_files.add(fid)
            elif "image_id" in payload:
                row = s.execute(
                    select(Image.file_id).where(
                        Image.id == int(payload["image_id"])
                    )
                ).first()
                if row is not None:
                    live_files.add(row[0])

        for f in candidates:
            if f.id in live_files:
                continue
            # Fast path: pending_embeds=0 with a CAS row means the prior run
            # already wrote text/chunks/images and the corresponding Qdrant
            # points (the decrement just got lost on the way out). Snap to
            # indexed instead of cycling through pending + a no-op extract
            # that would short-circuit on unchanged sha and leave the file
            # stuck in pending.
            if f.pending_embeds == 0 and f.file_cas_id is not None:
                logger.warning(
                    "reconcile: file_id=%d was stuck (state=%s pending=0) "
                    "— forcing indexed",
                    f.id,
                    f.state,
                )
                f.state = "indexed"
                f.error = None
                repaired_ids.append(f.id)
                continue
            logger.warning(
                "reset abandoned extract: file_id=%d state=%s pending=%d",
                f.id,
                f.state,
                f.pending_embeds,
            )
            f.state = "pending"
            f.pending_embeds = 0
            f.error = None
            # Re-enqueue an extract job; reclaim_abandoned_jobs already ran
            # so this won't dedup against the dead one.
            job_queue.enqueue(
                s, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
            )
            repaired_ids.append(f.id)

        # Pending-orphan sweep: files left in 'pending' with no live extract
        # job. Happens when a cancel-all from a previous run dropped every
        # queued extract; the file row keeps its 'pending' state but no
        # code path re-enqueues it (scanner only re-enqueues on mtime/size
        # change). Without this, a hand-rolled `cancel-all` mid-sync leaves
        # hundreds of files in permanent limbo.
        stranded_pending = list(
            s.execute(select(File).where(File.state == "pending")).scalars()
        )
        for f in stranded_pending:
            if f.id in live_files:
                continue
            logger.warning(
                "reconcile: re-enqueueing stranded pending file_id=%d", f.id
            )
            job_queue.enqueue(
                s, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
            )
            repaired_ids.append(f.id)
    for fid in repaired_ids:
        publish_file_upserted(fid)
    return len(repaired_ids)


HANDLERS = {
    "extract": run_extract,
    "embed_text": run_embed_text,
    "embed_image": run_embed_image,
    "delete_file": run_delete_file,
    "sync": run_sync,
    "reindex_folder": run_reindex_folder,
}
