"""The ``extract`` job handler and its end-to-end pipeline.

Parse → chunk → write CAS → commit rows → run text/image embeds inline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from sqlalchemy import select

from ...cas import store as cas_store
from ...config import get_settings
from ...db.database import session_scope
from ...db.models import Chunk, ChunkImageLink, File, Folder, Image
from ...logging_config import bind_context
from ..chunking import ChunkInfo, anchor_chunk_for_position, get_default_chunking_registry
from ..parsers.base import UnsupportedDocumentError
from ..parsers.registry import get_default_registry
from .common import (
    _EXTRACT_LOCK,
    _format_exception,
    _mark_error,
    _mark_state,
    _stage,
    logger,
    publish_file_upserted,
)

# extract runs text/image embeds INLINE (not via the job queue) — see
# _extract_resolved; do not "fix" this import away.
from .embed import _embed_image_sync, _embed_text_sync


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


def _export_native_doc(gdoc_path: Path) -> Path | None:
    """Opportunistically export a native Google doc to a parseable file.

    Reads the ``.gdoc/.gsheet/.gslides`` pointer for its ``doc_id`` and hits
    Google's *anonymous* export endpoint. This succeeds only for link-shared /
    public docs (private docs return 401) — exactly the no-credentials contract
    we want. On success the rendered bytes (PDF/XLSX) are written to a temp file
    whose suffix routes it to the right parser; the caller parses it and unlinks
    it. Returns ``None`` (→ link-only) on any failure, never raising.

    Note: we do NOT pass cookies/tokens — this is deliberately credential-free,
    so private docs stay link-only until the OAuth connector handles them.
    """
    import tempfile
    import urllib.request

    from ..sync.cloudstorage_local import read_gdoc_pointer

    ptr = read_gdoc_pointer(gdoc_path)
    if ptr is None or not ptr.doc_id:
        return None
    url = f"https://docs.google.com/{ptr.kind}/d/{ptr.doc_id}/export?format={ptr.export_format}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "voitta-rag/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed google host
            if resp.status != 200:
                return None
            data = resp.read()
    except Exception as e:  # noqa: BLE001 — network/auth failure → link-only
        logger.info("native-doc export skipped for %s: %s", gdoc_path.name, e)
        return None
    if not data or len(data) < 64:
        return None
    # An HTML body means we got a sign-in / error page, not the rendered doc.
    if data[:15].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        logger.info("native-doc export for %s returned HTML (not shared) — link-only", gdoc_path.name)
        return None
    try:
        fd, tmp = tempfile.mkstemp(suffix=f".{ptr.export_format}", prefix="voitta-gdoc-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError:
        return None
    logger.info(
        "native-doc export ok: %s → %s (%d bytes)",
        gdoc_path.name, ptr.export_format, len(data),
    )
    return Path(tmp)


def _run_extract_inner(file_id: int) -> None:
    logger.info("extract begin")

    # A scan may mark a file deleted while its extract job is still queued
    # (observed at scale: 1833 queued extracts pointing at deleted rows after
    # a Drive-mount rescan). Running them would re-read — and for cloud stubs
    # re-DOWNLOAD — content the app considers gone. Dead row ⇒ dead job.
    with _stage("check_row_state"):
        with session_scope() as s:
            file = s.get(File, file_id)
            state = file.state if file is not None else None
    if state is None or state == "deleted":
        logger.info(
            "extract abort: file row %s",
            "gone" if state is None else "already deleted",
        )
        return  # ``delete_file`` cleans up

    with _stage("resolve_path"):
        abs_path = _resolve_path(file_id)
    if abs_path is None:
        logger.info("extract abort: file or folder gone")
        return  # file or folder gone; ``delete_file`` cleans up

    if not abs_path.exists() or not abs_path.is_file():
        logger.info("extract abort: path missing on disk path=%s", abs_path)
        _mark_state(file_id, state="deleted")
        return

    # Native Google Workspace docs (.gdoc/.gsheet/.gslides) carry no usable
    # local bytes — the file is a JSON pointer. Try an anonymous export (works
    # for link-shared docs); parse the rendered copy if we get one, else leave
    # the file as link-only (its source_url is already set from the sync
    # sidecar) and mark it unsupported-with-reason rather than erroring.
    import contextlib as _contextlib

    from ..sync.cloudstorage_local import is_native_doc

    tmp_export: Path | None = None
    try:
        if is_native_doc(abs_path):
            tmp_export = _export_native_doc(abs_path)
            if tmp_export is None:
                _mark_state(
                    file_id,
                    state="unsupported",
                    error="native Google doc — indexed as link (not shared or export unavailable)",
                )
                return
            abs_path = tmp_export
        _extract_resolved(file_id, abs_path)
    finally:
        if tmp_export is not None:
            with _contextlib.suppress(OSError):
                tmp_export.unlink()


def _extract_resolved(file_id: int, abs_path: Path) -> None:
    extract_started = time.perf_counter()
    logger.debug("path=%s", abs_path)
    settings = get_settings()
    try:
        with _stage("stat"):
            stat = abs_path.stat()
    except OSError as e:
        _mark_error(file_id, f"stat failed: {e}")
        return
    from ..indexing_caps import DATA_EXTENSIONS, get_caps

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

    # Cloud placeholders (File Provider "dataless" files under
    # ~/Library/CloudStorage) have a size but no local bytes; read_bytes()
    # below would make the provider download the FULL content synchronously,
    # with no timeout — one 398 MB recording on slow Wi-Fi froze the whole
    # (single-worker) queue. Park them as unsupported-with-reason instead;
    # they index normally once the user materializes them ("Available
    # offline" in Drive) and reindexes, or opts in to downloads via
    # VOITTA_CLOUD_MATERIALIZE_ON_INDEX.
    from ..sync.cloudstorage_local import is_dataless_stub, is_within_cloud_storage

    if (
        not settings.cloud_materialize_on_index
        and is_dataless_stub(stat)
        and is_within_cloud_storage(abs_path)
    ):
        logger.info("cloud-only stub (size=%d, no local bytes) — not downloading", stat.st_size)
        _mark_state(
            file_id,
            state="unsupported",
            error=(
                "cloud-only file — content not on disk; make it available "
                "offline in Google Drive and reindex, or set "
                "VOITTA_CLOUD_MATERIALIZE_ON_INDEX=true to download while indexing"
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

    from ..meta_sidecar import load as load_meta_sidecar

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
    except UnsupportedDocumentError as e:
        # Well-formed but unindexable (e.g. password-protected PDF). Park it
        # in ``unsupported`` with the reason rather than counting it as an
        # error; reindex retries if the situation changes.
        logger.info("%s unsupported: %s", parser.__class__.__name__, e.reason)
        _mark_state(file_id, state="unsupported", error=e.reason)
        return
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
            from ..layout import summaries_by_page

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
    from .. import vector_store

    vector_store.remove_file_from_image_points(file_id)

    publish_file_upserted(_committed_file_id)
    return new_round, image_ids
