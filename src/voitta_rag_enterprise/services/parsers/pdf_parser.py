"""PDF parser backed by MinerU.

We delegate the actual extraction to MinerU's pipeline backend, which produces
much higher-quality markdown than per-page text concat (tables, formulas,
multi-column layouts, OCR for scanned pages). MinerU also writes the figures
it extracted into an ``images/`` directory beside the markdown, with the
markdown referencing them as ``![](images/<name>)`` — we lift those refs into
``ExtractedImage`` entries with their char offset in the produced markdown.

Concurrency
-----------
The whole MinerU call enters the process-wide GPU mutex (see
``services.gpu_lock``). Every other GPU consumer — text and image embedders
— enters the same mutex, so MinerU never competes with embedding for the
device, and only one inference task runs at a time across the whole
process.

Bucketing
---------
PDFs longer than ``pdf_pages_per_bucket * 1.2`` pages are split into
fixed-size chunks via PyMuPDF, parsed individually, and the markdown is
concatenated. Image positions in later buckets are offset by the cumulative
length of the prior buckets' markdown plus the ``\\n\\n`` joiner.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import mimetypes
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import fitz  # PyMuPDF — used only to count/split pages, not to extract text.

from ...config import get_settings
from ..gpu_lock import gpu_lock
from .base import BaseParser, ExtractedImage, ParserResult, RenderedPage

logger = logging.getLogger(__name__)

_MINERU_GPU_LOGGED = False

_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


@dataclass
class _BucketResult:
    markdown: str
    images_dir: Path  # absolute path; lives only inside the tempdir for this bucket
    page_start: int  # 1-indexed inclusive
    page_end: int  # 1-indexed inclusive
    elapsed_s: float
    # MinerU's per-block content list parsed from ``<name>_content_list.json``.
    # Each block dict carries at least ``type`` and ``page_idx`` (0-based,
    # local to the bucket). We only consult it to recover accurate page
    # numbers for image blocks — see ``_image_page_map``.
    content_list: list[dict]
    # Per-page renders captured by ``_render_pages``. Pages are 1-indexed and
    # absolute (``page_start`` already added in).
    page_renders: list[RenderedPage]


class PdfParser(BaseParser):
    extensions: ClassVar[list[str]] = [".pdf"]

    def parse(self, file_path: Path) -> ParserResult:
        settings = get_settings()
        if settings.use_fake_pdf_parser:
            return _fake_parse(file_path)

        page_count = _page_count(file_path)
        if page_count <= 0:
            return ParserResult.failure(f"could not open pdf: {file_path}")

        bucket_size = settings.pdf_pages_per_bucket
        threshold = int(bucket_size * 1.2)
        use_buckets = page_count > threshold
        logger.info(
            "pdf parse: %s pages=%d %s",
            file_path.name,
            page_count,
            f"bucketed({bucket_size})" if use_buckets else "single",
        )

        with tempfile.TemporaryDirectory(prefix="mineru-") as tmp:
            tmp_path = Path(tmp)
            if use_buckets:
                bucket_inputs = _split_pdf(file_path, tmp_path / "buckets", bucket_size)
            else:
                bucket_inputs = [(file_path, 1, page_count)]

            bucket_results: list[_BucketResult] = []
            for i, (bucket_path, start_page, end_page) in enumerate(bucket_inputs):
                logger.info(
                    "pdf bucket %d/%d: pages %d-%d",
                    i + 1,
                    len(bucket_inputs),
                    start_page,
                    end_page,
                )
                bucket_results.append(
                    _parse_bucket(
                        bucket_path,
                        out_root=tmp_path / f"out-{i:03d}",
                        method=settings.pdf_parse_method,
                        lang=settings.pdf_lang,
                        page_start=start_page,
                        page_end=end_page,
                    )
                )

            return _merge_buckets(bucket_results, file_path, page_count)


def _page_count(file_path: Path) -> int:
    doc = fitz.open(file_path)
    try:
        return len(doc)
    finally:
        doc.close()


def _split_pdf(
    src: Path, out_dir: Path, pages_per_bucket: int
) -> list[tuple[Path, int, int]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    src_doc = fitz.open(src)
    try:
        total = len(src_doc)
        buckets: list[tuple[Path, int, int]] = []
        for start in range(0, total, pages_per_bucket):
            end = min(start + pages_per_bucket, total)
            bucket_doc = fitz.open()
            try:
                bucket_doc.insert_pdf(src_doc, from_page=start, to_page=end - 1)
                bucket_path = out_dir / f"bucket_{start // pages_per_bucket + 1:03d}.pdf"
                bucket_doc.save(str(bucket_path))
            finally:
                bucket_doc.close()
            buckets.append((bucket_path, start + 1, end))
        return buckets
    finally:
        src_doc.close()


def _parse_bucket(
    bucket_path: Path,
    *,
    out_root: Path,
    method: str,
    lang: str,
    page_start: int,
    page_end: int,
) -> _BucketResult:
    """Run MinerU on a single PDF (or bucket) under the global GPU mutex.

    The parse runs in a long-lived MinerU subprocess (see ``_MineruDaemon``)
    so we can hard-kill it on timeout. The parent thread holds ``gpu_lock``
    for the entire subprocess call so the embedders in this process don't
    contend with the subprocess for the GPU — same serialisation guarantee
    we used to get from running MinerU in-process.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    pdf_name = bucket_path.stem
    timeout_s = get_settings().pdf_parse_timeout_s

    started = time.perf_counter()
    with gpu_lock("mineru.parse"):
        _log_gpu_once()
        _mineru_daemon().parse(
            bucket_path=bucket_path,
            out_root=out_root,
            method=method,
            lang=lang,
            pdf_name=pdf_name,
            timeout_s=timeout_s,
        )
    elapsed = time.perf_counter() - started

    md_path = out_root / pdf_name / method / f"{pdf_name}.md"
    if not md_path.exists():
        candidates = list(out_root.rglob("*.md"))
        if not candidates:
            raise RuntimeError(
                f"MinerU produced no markdown for {bucket_path} under {out_root}"
            )
        md_path = candidates[0]

    markdown = md_path.read_text(encoding="utf-8")
    images_dir = md_path.parent / "images"
    if not images_dir.exists():
        images_dir = md_path.parent  # we'll resolve refs against the md's dir

    content_list = _read_content_list(md_path.parent, pdf_name)
    page_renders = _render_pages(bucket_path, page_start)

    logger.info(
        "pdf bucket parsed: pages=%d-%d chars=%d renders=%d in %.1fs",
        page_start,
        page_end,
        len(markdown),
        len(page_renders),
        elapsed,
    )
    return _BucketResult(
        markdown=markdown,
        images_dir=images_dir,
        page_start=page_start,
        page_end=page_end,
        elapsed_s=elapsed,
        content_list=content_list,
        page_renders=page_renders,
    )


def _read_content_list(md_dir: Path, pdf_name: str) -> list[dict]:
    """Load MinerU's ``<name>_content_list.json``. Returns ``[]`` if missing.

    The file is the only output that retains per-block page numbers under
    the pipeline backend, which we use to fix ``ExtractedImage.page`` (the
    .md alone is page-agnostic).
    """
    candidate = md_dir / f"{pdf_name}_content_list.json"
    if not candidate.exists():
        # Defensive fallback for layout drift: pick any *_content_list.json
        # under md_dir before giving up.
        candidates = list(md_dir.rglob("*_content_list.json"))
        if not candidates:
            logger.warning("pdf bucket: content_list.json missing under %s", md_dir)
            return []
        candidate = candidates[0]
    try:
        with candidate.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("pdf bucket: content_list.json unreadable: %s", e)
        return []
    return data if isinstance(data, list) else []


def _render_pages(pdf_path: Path, page_start: int) -> list[RenderedPage]:
    """Rasterise every page of ``pdf_path`` to WebP for layout context.

    ``page_start`` is the 1-indexed absolute page of this bucket's first
    page; we add it to the local index so callers see absolute numbers.
    Skips silently if rendering is disabled or PIL/WebP is unavailable.

    The output sits intentionally low-res: the LLM has the per-figure
    crops + the full markdown already; these renders only carry layout
    cues, so quality 75 at ~1024px is plenty.
    """
    settings = get_settings()
    if not settings.pdf_render_pages:
        return []
    long_edge = max(64, int(settings.pdf_page_render_long_edge_px))
    quality = max(1, min(100, int(settings.pdf_page_render_webp_quality)))
    try:
        from PIL import Image as PILImage
    except ImportError:
        logger.warning("PIL missing — skipping page renders")
        return []

    pages: list[RenderedPage] = []
    doc = fitz.open(pdf_path)
    try:
        for local_idx in range(len(doc)):
            page = doc.load_page(local_idx)
            rect = page.rect
            page_long = max(rect.width, rect.height)
            if page_long <= 0:
                continue
            # Pick a render scale that targets ``long_edge`` on the long axis;
            # PyMuPDF measures pages in PDF points (72 dpi), so dpi = 72*scale.
            scale = long_edge / page_long
            matrix = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            try:
                pil = PILImage.frombytes(
                    "RGB", (pix.width, pix.height), pix.samples
                )
                buf = io.BytesIO()
                pil.save(buf, format="WEBP", quality=quality, method=6)
                data = buf.getvalue()
            finally:
                pix = None  # type: ignore[assignment]
            pages.append(
                RenderedPage(
                    bytes=data,
                    mime="image/webp",
                    page=page_start + local_idx,
                    width=pil.width,
                    height=pil.height,
                )
            )
    finally:
        doc.close()
    return pages


def _merge_buckets(
    buckets: list[_BucketResult], file_path: Path, page_count: int
) -> ParserResult:
    parts: list[str] = []
    images: list[ExtractedImage] = []
    page_images: list[RenderedPage] = []
    page_layout: list[dict] = []
    char_to_page: list[tuple[int, int]] = []
    cursor = 0  # char offset into the joined markdown for the current bucket

    join_sep = "\n\n"
    for i, b in enumerate(buckets):
        # Bucket-local map: image filename → absolute (1-indexed) page,
        # derived from MinerU's content_list.json. Missing keys fall back
        # to the bucket's first page (the old, less-accurate behavior).
        page_by_name = _image_page_map(b.content_list, b.page_start)
        page_layout.extend(_layout_for_bucket(b.content_list, b.page_start))
        char_to_page.extend(
            _bucket_char_to_page(b.content_list, b.markdown, b.page_start, cursor)
        )

        # Walk image references in the bucket's markdown — their char offsets
        # are absolute in the merged output once we add ``cursor``.
        for m in _IMG_REF_RE.finditer(b.markdown):
            ref = m.group(1).strip()
            img_path = _resolve_image_path(b.images_dir, ref)
            if img_path is None or not img_path.exists():
                logger.warning(
                    "pdf bucket image missing: ref=%r dir=%s — skipping",
                    ref,
                    b.images_dir,
                )
                continue
            try:
                data = img_path.read_bytes()
            except OSError as e:
                logger.warning("pdf image read failed: %s — %s", img_path, e)
                continue
            width, height = _dimensions(data)
            mime = mimetypes.guess_type(img_path.name)[0] or "application/octet-stream"
            page = page_by_name.get(img_path.name, b.page_start)
            images.append(
                ExtractedImage(
                    bytes=data,
                    mime=mime,
                    position=cursor + m.start(),
                    page=page,
                    width=width,
                    height=height,
                )
            )

        page_images.extend(b.page_renders)

        parts.append(b.markdown)
        cursor += len(b.markdown)
        if i < len(buckets) - 1:
            cursor += len(join_sep)

    content = join_sep.join(parts)
    char_to_page = _normalise_char_to_page(char_to_page, len(content))
    metadata = {
        "source_format": "pdf",
        "parser": "mineru",
        "filename": file_path.name,
        "buckets": len(buckets),
        "page_count": page_count,
        "parse_time_seconds": sum(b.elapsed_s for b in buckets),
        "page_images": len(page_images),
        "page_layout_blocks": len(page_layout),
        "char_to_page_anchors": len(char_to_page),
    }
    return ParserResult(
        content=content,
        images=images,
        page_images=page_images,
        page_layout=page_layout,
        char_to_page=char_to_page,
        metadata=metadata,
    )


def _bucket_char_to_page(
    content_list: list[dict],
    bucket_md: str,
    page_start: int,
    cursor: int,
) -> list[tuple[int, int]]:
    """Locate each text/title block from ``content_list`` in ``bucket_md``
    and emit ``(global_char_offset, absolute_page)`` anchors.

    MinerU writes blocks in reading order, so we walk ``content_list`` in
    order and search forward through ``bucket_md`` from a moving cursor.
    Blocks whose text doesn't match (whitespace normalisation, hyphen
    collapse, formula re-rendering, …) are silently skipped — neighbours
    bracket their pages, and the worst case the consumer suffers is a
    paragraph ending up ±1 page off, which the dev plan explicitly
    accepts.

    Image / table / equation blocks have no usable ``text`` to find, so
    we skip them; the ``page_layout`` list separately records them for
    layout-summary purposes.
    """
    anchors: list[tuple[int, int]] = []
    bucket_search_pos = 0
    last_page = page_start  # so the very start of the bucket has a page
    anchors.append((cursor, last_page))
    for blk in content_list:
        if not isinstance(blk, dict):
            continue
        page_idx = blk.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        abs_page = page_start + page_idx
        text = blk.get("text") if blk.get("type") in ("text", "title") else None
        if not isinstance(text, str) or len(text.strip()) < 8:
            continue
        # MinerU sometimes wraps text in markdown decoration (## title).
        # Strip leading hash-decoration so the find still works.
        needle = text.strip()
        # Cap the needle so a 4 KB paragraph doesn't pathologically scan.
        # First 80 chars are usually unique enough across a bucket.
        probe = needle[:80]
        idx = bucket_md.find(probe, bucket_search_pos)
        if idx == -1:
            continue
        anchors.append((cursor + idx, abs_page))
        bucket_search_pos = idx + len(probe)
        last_page = abs_page
    return anchors


def _normalise_char_to_page(
    anchors: list[tuple[int, int]], content_len: int
) -> list[tuple[int, int]]:
    """Collapse adjacent anchors with the same page; drop out-of-range entries.

    The result is sorted by char offset (anchors are already produced in
    order, but we re-sort defensively in case bucket boundaries land
    between buckets out of order). Consumers binary-search this list.
    """
    if not anchors:
        return []
    anchors_sorted = sorted({(o, p) for (o, p) in anchors if 0 <= o < content_len + 1})
    out: list[tuple[int, int]] = []
    for offset, page in anchors_sorted:
        if out and out[-1][1] == page:
            continue  # same page as previous anchor — redundant
        out.append((offset, page))
    return out


def _layout_for_bucket(content_list: list[dict], page_start: int) -> list[dict]:
    """Normalise MinerU's per-bucket content list for global storage.

    MinerU writes ``page_idx`` as 0-based and local to the parsed PDF
    (each bucket sees pages 0..N-1). We translate to absolute 1-indexed
    pages and store the result under a new ``page`` key, keeping
    ``page_idx`` around in case a downstream wants the raw value.
    Anything else MinerU emits (``type``, ``bbox``, ``text``,
    ``text_level``, ``img_path``, ``img_caption``, ``table_caption``,
    ``table_body`` for tables, etc.) is passed through unchanged.

    Blocks without an int ``page_idx`` are dropped — they would land on
    an unknown page and only confuse the consumer.
    """
    out: list[dict] = []
    for blk in content_list:
        if not isinstance(blk, dict):
            continue
        page_idx = blk.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        merged = dict(blk)
        merged["page"] = page_start + page_idx
        out.append(merged)
    return out


def _image_page_map(content_list: list[dict], page_start: int) -> dict[str, int]:
    """Map ``image_basename → absolute_page`` from MinerU's content list.

    The blob's ``page_idx`` is 0-based and local to the bucket; we add
    ``page_start - 1`` to land on absolute 1-indexed pages. Both
    ``type='image'`` and ``type='table'`` blocks expose ``img_path``.
    """
    out: dict[str, int] = {}
    for blk in content_list:
        if not isinstance(blk, dict):
            continue
        path = blk.get("img_path")
        if not isinstance(path, str) or not path:
            continue
        page_idx = blk.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        # MinerU writes ``img_path`` as either ``foo.jpg`` or
        # ``images/foo.jpg`` — the basename is the stable key.
        name = path.rsplit("/", 1)[-1]
        out[name] = page_start + page_idx
    return out


def _resolve_image_path(images_dir: Path, ref: str) -> Path | None:
    """Map an ``![](...)`` reference to a file on disk under the bucket output."""
    candidate = (images_dir / ref).resolve()
    if candidate.exists():
        return candidate
    # MinerU sometimes writes refs as ``images/foo.jpg`` while images_dir is
    # already ``.../images`` — strip the leading ``images/`` and retry.
    stripped = ref.split("/", 1)[-1]
    fallback = (images_dir / stripped).resolve()
    if fallback.exists():
        return fallback
    sibling = (images_dir.parent / ref).resolve()
    if sibling.exists():
        return sibling
    return None


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return (None, None)


# ---------------------------------------------------------------------------
# MinerU subprocess daemon
# ---------------------------------------------------------------------------


class _MineruDaemon:
    """Persistent MinerU subprocess. Talks JSON over stdio.

    Why this exists: see ``_mineru_subprocess`` module docstring. tl;dr —
    MinerU has been observed to hang inside native code on certain PDFs;
    Python signals can't unwedge a blocked C thread, so we isolate the
    parse in a subprocess and SIGKILL it on timeout.

    Concurrency contract: ``_lock`` serialises every parse so we never
    interleave two requests on the same stdio. Together with the parent's
    ``gpu_lock``, this guarantees a single MinerU call at a time.

    Lifecycle: lazy-spawn on first ``parse``; respawn after a kill or any
    exit. The first request after a respawn pays MinerU's model-load cost
    (~5s); subsequent requests reuse the warm process.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _spawn(self) -> None:
        # Use the same Python that's running the parent so the subprocess
        # picks up our installed venv (MinerU and torch live there).
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "voitta_rag_enterprise.services.parsers._mineru_subprocess"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,  # line-buffered — we round-trip one JSON line per request
            text=True,
        )
        # MinerU's progress bars and torch CUDA messages go to stderr. Drain
        # the pipe on a background thread so a chatty parse doesn't fill the
        # pipe buffer and deadlock; surface each line to our logger so the
        # operator can see what the subprocess is doing.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._proc.stderr,),
            daemon=True,
            name="mineru-stderr",
        )
        self._stderr_thread.start()
        logger.info("MinerU daemon spawned: pid=%d", self._proc.pid)

    @staticmethod
    def _drain_stderr(stream) -> None:
        try:
            for line in stream:
                line = line.rstrip()
                if line:
                    logger.info("[mineru] %s", line)
        except Exception:
            logger.exception("MinerU stderr drainer crashed")

    def _ensure_alive(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._proc is not None:
            logger.warning(
                "MinerU daemon exited (rc=%s); respawning", self._proc.returncode
            )
        self._spawn()

    def _kill(self, reason: str) -> None:
        proc = self._proc
        if proc is None:
            return
        logger.error("killing MinerU daemon pid=%d: %s", proc.pid, reason)
        with contextlib.suppress(OSError):
            proc.kill()
        # Reap the zombie so the next ``poll()`` reports it as dead.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)

    def parse(
        self,
        *,
        bucket_path: Path,
        out_root: Path,
        method: str,
        lang: str,
        pdf_name: str,
        timeout_s: int,
    ) -> None:
        with self._lock:
            self._ensure_alive()
            assert self._proc is not None
            request = (
                json.dumps(
                    {
                        "bucket_path": str(bucket_path),
                        "out_root": str(out_root),
                        "method": method,
                        "lang": lang,
                        "pdf_name": pdf_name,
                    }
                )
                + "\n"
            )
            try:
                self._proc.stdin.write(request)  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError) as e:
                # Subprocess died between ensure_alive and write. Respawn
                # and surface as a transient error so the job can be retried.
                self._kill("broken pipe before request")
                raise RuntimeError(f"MinerU daemon broken pipe: {e}") from e

            # Watchdog: if no response in `timeout_s` we kill the subprocess.
            # Killing closes our stdout pipe, which unblocks the readline()
            # below with an empty string — that's how the timeout is
            # delivered to the calling thread.
            done = threading.Event()

            def _watchdog() -> None:
                if not done.wait(timeout_s):
                    self._kill(f"watchdog: no response in {timeout_s}s")

            wt = threading.Thread(target=_watchdog, daemon=True, name="mineru-watchdog")
            wt.start()

            try:
                resp_line = self._proc.stdout.readline()  # type: ignore[union-attr]
            finally:
                done.set()

            if not resp_line:
                # Either watchdog killed it or the process crashed on its own.
                # Treat both as a timeout from the caller's perspective —
                # they get a TimeoutError, the file is parked as 'error' in
                # the indexer, and the queue moves on.
                raise TimeoutError(
                    f"MinerU parse exceeded {timeout_s}s on {bucket_path.name}"
                )

            try:
                resp = json.loads(resp_line)
            except json.JSONDecodeError as e:
                self._kill("malformed response")
                raise RuntimeError(
                    f"MinerU daemon returned non-JSON: {resp_line[:200]!r}"
                ) from e

            if resp.get("status") != "ok":
                detail = resp.get("detail") or "unknown error"
                # Pass the subprocess traceback through to our log; the
                # exception itself stays terse so it fits in the file.error
                # column without a wall of stack.
                if "traceback" in resp:
                    logger.error("MinerU subprocess error:\n%s", resp["traceback"])
                raise RuntimeError(f"MinerU error: {detail}")


_DAEMON: _MineruDaemon | None = None
_DAEMON_LOCK = threading.Lock()


def _mineru_daemon() -> _MineruDaemon:
    """Return the process-wide daemon, spawning the wrapper class on first call."""
    global _DAEMON
    if _DAEMON is None:
        with _DAEMON_LOCK:
            if _DAEMON is None:
                _DAEMON = _MineruDaemon()
    return _DAEMON


def reset_mineru_daemon_for_tests() -> None:
    """Tear down any running daemon. Used by the test suite to swap in a stub."""
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is not None:
            _DAEMON._kill("reset_for_tests")
        _DAEMON = None


def _log_gpu_once() -> None:
    """First time MinerU is invoked, log whether CUDA is visible."""
    global _MINERU_GPU_LOGGED
    if _MINERU_GPU_LOGGED:
        return
    _MINERU_GPU_LOGGED = True
    try:
        import torch

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            logger.info(
                "MinerU: CUDA available — device 0: %s (count=%d)",
                name,
                torch.cuda.device_count(),
            )
        else:
            logger.warning("MinerU: CUDA not available — running on CPU")
    except Exception as e:
        logger.warning("MinerU: torch probe failed: %s", e)


def _fake_parse(file_path: Path) -> ParserResult:
    """Deterministic stub used by the test suite (``VOITTA_USE_FAKE_PDF_PARSER``).

    Reads the raw bytes so the file_cas hash differs across distinct fixtures,
    and returns a trivial markdown body with no images.
    """
    raw = file_path.read_bytes()
    content = f"# {file_path.stem}\n\n[fake-pdf-parser bytes={len(raw)}]\n"
    return ParserResult(
        content=content,
        images=[],
        metadata={"source_format": "pdf", "parser": "fake", "filename": file_path.name},
    )
