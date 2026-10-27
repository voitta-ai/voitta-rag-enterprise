"""PDF parser backed by MinerU.

We delegate the actual extraction to MinerU's ``basic`` tier (layout, OCR,
formula and table models; no VLM), which produces much higher-quality
markdown than per-page text concat (tables, formulas, multi-column layouts,
OCR for scanned pages). MinerU also writes the figures it extracted into an
``images/`` directory beside the markdown, with the markdown referencing
them as ``![](images/<name>)`` — we lift those refs into ``ExtractedImage``
entries with their char offset in the produced markdown. Reading MinerU's
saved output lives in ``mineru_result``.

Concurrency
-----------
The whole MinerU call enters the process-wide GPU mutex (see
``services.gpu_lock``). Every other GPU consumer — text and image embedders
— enters the same mutex, so MinerU never competes with embedding for the
device, and only one inference task runs at a time across the whole
process.

Bucketing
---------
PDFs longer than ``pdf_pages_per_bucket * 1.2`` pages are parsed in
fixed-size page ranges (MinerU's ``page_range``, on the original file), one
daemon request per range, so the watchdog timeout and progress logging are
per bucket. MinerU reports ``page_idx`` against the original document, so
pages need no per-bucket offset; the markdown is concatenated and image and
page anchors are offset by the cumulative length of the prior buckets'
markdown plus the ``\\n\\n`` joiner.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import mimetypes
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import fitz  # PyMuPDF — page counts and page renders, not text extraction.

from ...config import get_settings
from ..gpu_lock import gpu_lock
from ..indexing_caps import get_caps
from . import mineru_result
from .base import (
    BaseParser,
    ExtractedImage,
    ParserResult,
    RenderedPage,
    UnsupportedDocumentError,
)

logger = logging.getLogger(__name__)

_MINERU_GPU_LOGGED = False

_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# MinerU's own render-timeout default; we only ever lower it (see _spawn).
_MINERU_RENDER_TIMEOUT_S = 300


def _render_timeout_s() -> int:
    """MinerU's render timeout for the current parse budget: its own 300 s,
    or 80 % of ``pdf_parse_timeout_s`` when that is lower, so a stuck render
    fails as a MinerU error before our watchdog has to SIGKILL the daemon."""
    return max(1, min(_MINERU_RENDER_TIMEOUT_S, int(get_caps().pdf_parse_timeout_s * 0.8)))


@dataclass
class _BucketResult:
    saved: mineru_result.SavedResult
    page_start: int  # 1-indexed inclusive
    page_end: int  # 1-indexed inclusive
    elapsed_s: float
    # Per-page renders captured by ``_render_pages``; pages are 1-indexed.
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

        bucket_size = get_caps().pdf_pages_per_bucket
        threshold = int(bucket_size * 1.2)
        use_buckets = page_count > threshold
        logger.info(
            "pdf parse: %s pages=%d %s",
            file_path.name,
            page_count,
            f"bucketed({bucket_size})" if use_buckets else "single",
        )
        if use_buckets:
            ranges = [
                (start, min(start + bucket_size - 1, page_count))
                for start in range(1, page_count + 1, bucket_size)
            ]
        else:
            ranges = [(1, page_count)]

        with tempfile.TemporaryDirectory(prefix="mineru-") as tmp:
            tmp_path = Path(tmp)
            bucket_results: list[_BucketResult] = []
            for i, (start_page, end_page) in enumerate(ranges):
                logger.info(
                    "pdf bucket %d/%d: pages %d-%d",
                    i + 1,
                    len(ranges),
                    start_page,
                    end_page,
                )
                bucket_results.append(
                    _parse_bucket(
                        file_path,
                        out_dir=tmp_path / f"out-{i:03d}",
                        # Whole-document parses send no range, so MinerU
                        # reads the file as-is instead of rewriting it.
                        page_range=f"{start_page}-{end_page}" if use_buckets else "",
                        tier=settings.pdf_parse_tier,
                        parse_mode=settings.pdf_parse_method,
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


def _parse_bucket(
    pdf_path: Path,
    *,
    out_dir: Path,
    page_range: str,
    tier: str,
    parse_mode: str,
    page_start: int,
    page_end: int,
) -> _BucketResult:
    """Run MinerU on one page range of a PDF under the global GPU mutex.

    The parse runs in a long-lived MinerU subprocess (see ``_MineruDaemon``)
    so we can hard-kill it on timeout. The parent thread holds ``gpu_lock``
    for the entire subprocess call so the embedders in this process don't
    contend with the subprocess for the GPU — same serialisation guarantee
    we used to get from running MinerU in-process.
    """
    timeout_s = get_caps().pdf_parse_timeout_s

    started = time.perf_counter()
    with gpu_lock("mineru.parse"):
        _log_gpu_once()
        _mineru_daemon().parse(
            pdf_path=pdf_path,
            page_range=page_range,
            out_dir=out_dir,
            tier=tier,
            parse_mode=parse_mode,
            timeout_s=timeout_s,
        )
    elapsed = time.perf_counter() - started

    saved = mineru_result.load_saved_result(out_dir)
    page_renders = _render_pages(pdf_path, page_start, page_end)

    logger.info(
        "pdf bucket parsed: pages=%d-%d chars=%d renders=%d in %.1fs",
        page_start,
        page_end,
        len(saved.markdown),
        len(page_renders),
        elapsed,
    )
    return _BucketResult(
        saved=saved,
        page_start=page_start,
        page_end=page_end,
        elapsed_s=elapsed,
        page_renders=page_renders,
    )


def _render_pages(pdf_path: Path, page_start: int, page_end: int) -> list[RenderedPage]:
    """Rasterise pages ``page_start..page_end`` (1-indexed, inclusive) to WebP.

    Skips silently if rendering is disabled or PIL/WebP is unavailable.

    The output sits intentionally low-res: the LLM has the per-figure
    crops + the full markdown already; these renders only carry layout
    cues, so quality 75 at ~1024px is plenty.
    """
    if not get_settings().pdf_render_pages:
        return []
    caps = get_caps()
    long_edge = max(64, int(caps.pdf_page_render_long_edge_px))
    quality = max(1, min(100, int(caps.pdf_page_render_webp_quality)))
    try:
        from PIL import Image as PILImage
    except ImportError:
        logger.warning("PIL missing — skipping page renders")
        return []

    pages: list[RenderedPage] = []
    doc = fitz.open(pdf_path)
    try:
        for page_no in range(page_start, min(page_end, len(doc)) + 1):
            page = doc.load_page(page_no - 1)
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
                    page=page_no,
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
        markdown = b.saved.markdown
        content_list = b.saved.content_list
        # img_path → absolute page, from MinerU's content list. Refs missing
        # from it fall back to the bucket's first page.
        page_by_ref = mineru_result.image_page_map(content_list)
        page_layout.extend(mineru_result.layout_blocks(content_list))
        char_to_page.extend(
            mineru_result.char_to_page_anchors(content_list, markdown, b.page_start, cursor)
        )

        # Walk image references in the bucket's markdown — their char offsets
        # are absolute in the merged output once we add ``cursor``.
        for m in _IMG_REF_RE.finditer(markdown):
            ref = m.group(1).strip()
            img_path = mineru_result.resolve_image(b.saved.root, ref)
            if img_path is None:
                logger.warning(
                    "pdf bucket image missing: ref=%r dir=%s — skipping",
                    ref,
                    b.saved.root,
                )
                continue
            try:
                data = img_path.read_bytes()
            except OSError as e:
                logger.warning("pdf image read failed: %s — %s", img_path, e)
                continue
            width, height = _dimensions(data)
            mime = mimetypes.guess_type(img_path.name)[0] or "application/octet-stream"
            images.append(
                ExtractedImage(
                    bytes=data,
                    mime=mime,
                    position=cursor + m.start(),
                    page=page_by_ref.get(ref, b.page_start),
                    width=width,
                    height=height,
                )
            )

        page_images.extend(b.page_renders)

        parts.append(markdown)
        cursor += len(markdown)
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
        # The render timeout the live daemon was started with; None when the
        # operator fixed MINERU_PDF_RENDER_TIMEOUT in the environment.
        self._render_timeout: int | None = None

    def _spawn(self) -> None:
        # Use the same Python that's running the parent so the subprocess
        # picks up our installed venv (MinerU and torch live there).
        env = {**os.environ}
        if sys.platform == "darwin":
            # MinerU batches a window of pages (default 64) into one
            # inference pass — every page's layout/OCR/table/formula tensors
            # resident at once. On CUDA that's throttled by its VRAM probe,
            # but the probe has no MPS branch, so on Apple Silicon a
            # figure-dense 17-page PDF blows past available memory and jetsam
            # SIGKILLs the worker (rc=-9). Page-at-a-time keeps peak memory
            # flat; output is identical.
            # setdefault: an operator's explicit env override still wins.
            env.setdefault("MINERU_PROCESSING_WINDOW_SIZE", "1")
        # Page rendering runs in spawn-started worker processes (up to 3, one
        # per 30 pages). The daemon parses one bucket of ≤ ~24 pages at a
        # time, so one render process is all it can use; say so explicitly.
        env.setdefault("MINERU_PDF_RENDER_THREADS", "1")
        # Let MinerU's own render timeout fire before our watchdog does, so a
        # stuck render surfaces as a typed error instead of a SIGKILL. The
        # value is fixed for the daemon's life; _ensure_alive respawns it when
        # an admin changes pdf_parse_timeout_s.
        if "MINERU_PDF_RENDER_TIMEOUT" in env:
            self._render_timeout = None
        else:
            self._render_timeout = _render_timeout_s()
            env["MINERU_PDF_RENDER_TIMEOUT"] = str(self._render_timeout)
        # Shared GPU: cap the VRAM MinerU plans its batches for (it otherwise
        # assumes the whole card and OOMs beside other GPU tenants).
        # setdefault: an explicit MINERU_VIRTUAL_VRAM_SIZE in the env wins.
        vram_gb = get_settings().mineru_vram_gb
        if vram_gb > 0:
            env.setdefault("MINERU_VIRTUAL_VRAM_SIZE", str(vram_gb))
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "voitta_rag_enterprise.services.parsers._mineru_subprocess"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,  # line-buffered — we round-trip one JSON line per request
            text=True,
            env=env,
            # Own process group, so _kill takes MinerU's spawn render workers
            # down with the daemon instead of orphaning them mid-render.
            start_new_session=True,
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
            if self._render_timeout is None or self._render_timeout == _render_timeout_s():
                return
            # pdf_parse_timeout_s changed since spawn; the daemon's render
            # timeout must follow it. It is idle here (we hold _lock).
            self._kill("parse timeout changed; respawning with a new render timeout")
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
        # The whole process group: the daemon plus its render workers.
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.kill()
        # Reap it so the next ``poll()`` reports it as dead. A SIGKILLed CUDA
        # process can take a while to exit, and until it is reaped ``poll()``
        # says "alive" — so if it will not go, drop the handle and let the
        # next request spawn a fresh daemon rather than write into a corpse.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("MinerU daemon pid=%d still not reaped after SIGKILL; abandoning it", proc.pid)
            if self._proc is proc:
                self._proc = None

    def _reap_after_pipe_closed(self) -> int | None:
        """Wait for the daemon whose stdout just closed; return its exit code.

        A closed pipe means the process is dead or dying, but a SIGKILLed
        CUDA process can take seconds to exit, and ``Popen.poll()`` reports
        ``None`` (alive) while it is unreaped — or while the killing thread
        is still inside ``wait()``. Without this, the next request found the
        corpse "alive", wrote into it and failed an innocent file with a
        broken pipe. If it will not die, abandon the handle so the next
        request spawns a fresh daemon.
        """
        proc = self._proc
        if proc is None:
            return None
        try:
            return proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.error("MinerU daemon pid=%d did not exit after its pipe closed; abandoning it", proc.pid)
            self._proc = None
            return None

    def parse(
        self,
        *,
        pdf_path: Path,
        page_range: str,
        out_dir: Path,
        tier: str,
        parse_mode: str,
        timeout_s: int,
    ) -> None:
        label = f"{pdf_path.name}" + (f" pages {page_range}" if page_range else "")
        with self._lock:
            self._ensure_alive()
            assert self._proc is not None
            request = (
                json.dumps(
                    {
                        "pdf_path": str(pdf_path),
                        "page_range": page_range,
                        "out_dir": str(out_dir),
                        "tier": tier,
                        "parse_mode": parse_mode,
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
            timed_out = threading.Event()

            def _watchdog() -> None:
                if not done.wait(timeout_s):
                    timed_out.set()
                    self._kill(f"watchdog: no response in {timeout_s}s")

            wt = threading.Thread(target=_watchdog, daemon=True, name="mineru-watchdog")
            wt.start()

            try:
                resp_line = self._proc.stdout.readline()  # type: ignore[union-attr]
            finally:
                done.set()
                # If the watchdog fired, let its kill finish before we look
                # at the process.
                wt.join()

            if not resp_line:
                rc = self._reap_after_pipe_closed()
                # Empty read = the pipe closed. Two distinct causes, and the
                # stored file.error must not conflate them (a 34s OOM kill
                # logged as "exceeded 600s" sent diagnosis down the wrong
                # path): the watchdog killed it (real timeout), or the
                # process died on its own — rc=-9 is the OS killing it under
                # memory pressure.
                if timed_out.is_set():
                    raise TimeoutError(
                        f"MinerU parse exceeded {timeout_s}s on {label}"
                    )
                raise RuntimeError(
                    f"MinerU worker died mid-parse (rc={rc}) on {label}"
                    + (
                        " — SIGKILL, likely the OS reclaiming memory (OOM)"
                        if rc == -9
                        else ""
                    )
                )

            try:
                resp = json.loads(resp_line)
            except json.JSONDecodeError as e:
                self._kill("malformed response")
                raise RuntimeError(
                    f"MinerU daemon returned non-JSON: {resp_line[:200]!r}"
                ) from e

            if resp.get("status") != "ok":
                code = resp.get("code")
                detail = resp.get("detail") or "unknown error"
                tb = resp.get("traceback") or ""
                # A password-protected PDF is not an indexer failure — we
                # have no password to supply. Surface it as unsupported so it
                # parks cleanly instead of counting as an error.
                if code == "file_encrypted":
                    raise UnsupportedDocumentError(
                        "password-protected PDF — cannot decrypt without a password"
                    )
                if tb:
                    logger.error("MinerU subprocess error:\n%s", tb)
                # repr() of an OSError DROPS the filename argument, so
                # ``detail`` alone can read "FileNotFoundError(2, 'No such
                # file or directory')" with no path — undiagnosable from the
                # file.error column. Append the last traceback lines (the
                # raise site and exception message, which carry the path)
                # so the stored error is self-sufficient.
                tail = "\n".join(tb.strip().splitlines()[-6:])
                raise RuntimeError(
                    f"MinerU error{f' [{code}]' if code else ''}: {detail}"
                    + (f"\n--- subprocess traceback tail ---\n{tail}" if tail else "")
                )


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


def stop_mineru_daemon(reason: str = "shutdown") -> None:
    """Kill the daemon (and its render workers) if one is running.

    Called on app shutdown: the daemon runs in its own process group, so it
    does not receive the server's terminal signals, and a worker thread
    blocked on an in-flight parse would otherwise keep the interpreter alive
    until that parse finished.
    """
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is not None:
            _DAEMON._kill(reason)
        _DAEMON = None


def reset_mineru_daemon_for_tests() -> None:
    """Tear down any running daemon. Used by the test suite to swap in a stub."""
    stop_mineru_daemon("reset_for_tests")


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
