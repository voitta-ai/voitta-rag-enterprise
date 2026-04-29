"""PDF parser backed by MinerU.

We delegate the actual extraction to MinerU's pipeline backend, which produces
much higher-quality markdown than per-page text concat (tables, formulas,
multi-column layouts, OCR for scanned pages). MinerU also writes the figures
it extracted into an ``images/`` directory beside the markdown, with the
markdown referencing them as ``![](images/<name>)`` — we lift those refs into
``ExtractedImage`` entries with their char offset in the produced markdown.

Concurrency
-----------
The whole MinerU call is serialized behind ``_MINERU_LOCK`` so only one PDF
parse runs at any given moment in this process. MinerU loads several heavy ML
models (layout, OCR, formula, table) on first call and keeps them in
module-level globals; running multiple parses concurrently would either OOM,
duplicate the model weights, or thrash the GPU. Per project guidance, we rely
on serialization rather than a separate model-server process.

Bucketing
---------
PDFs longer than ``pdf_pages_per_bucket * 1.2`` pages are split into
fixed-size chunks via PyMuPDF, parsed individually, and the markdown is
concatenated. Image positions in later buckets are offset by the cumulative
length of the prior buckets' markdown plus the ``\\n\\n`` joiner.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import fitz  # PyMuPDF — used only to count/split pages, not to extract text.

from ...config import get_settings
from .base import BaseParser, ExtractedImage, ParserResult

logger = logging.getLogger(__name__)

_MINERU_LOCK = threading.Lock()
_MINERU_GPU_LOGGED = False

_IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


@dataclass
class _BucketResult:
    markdown: str
    images_dir: Path  # absolute path; lives only inside the tempdir for this bucket
    page_start: int  # 1-indexed inclusive
    page_end: int  # 1-indexed inclusive
    elapsed_s: float


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
    """Run MinerU on a single PDF (or bucket) under ``_MINERU_LOCK``."""
    out_root.mkdir(parents=True, exist_ok=True)
    pdf_name = bucket_path.stem

    started = time.perf_counter()
    with _MINERU_LOCK:
        _log_gpu_once()
        from mineru.cli.common import do_parse, read_fn  # imported lazily

        pdf_bytes = read_fn(bucket_path)
        do_parse(
            output_dir=str(out_root),
            pdf_file_names=[pdf_name],
            pdf_bytes_list=[pdf_bytes],
            p_lang_list=[lang],
            backend="pipeline",
            parse_method=method,
            formula_enable=True,
            table_enable=True,
            f_draw_layout_bbox=False,
            f_draw_span_bbox=False,
            f_dump_md=True,
            f_dump_middle_json=False,
            f_dump_model_output=False,
            f_dump_orig_pdf=False,
            f_dump_content_list=False,
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
    logger.info(
        "pdf bucket parsed: pages=%d-%d chars=%d in %.1fs",
        page_start,
        page_end,
        len(markdown),
        elapsed,
    )
    return _BucketResult(
        markdown=markdown,
        images_dir=images_dir,
        page_start=page_start,
        page_end=page_end,
        elapsed_s=elapsed,
    )


def _merge_buckets(
    buckets: list[_BucketResult], file_path: Path, page_count: int
) -> ParserResult:
    parts: list[str] = []
    images: list[ExtractedImage] = []
    cursor = 0  # char offset into the joined markdown for the current bucket

    join_sep = "\n\n"
    for i, b in enumerate(buckets):
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
            images.append(
                ExtractedImage(
                    bytes=data,
                    mime=mime,
                    position=cursor + m.start(),
                    page=b.page_start,  # MinerU output isn't per-page; use bucket start
                    width=width,
                    height=height,
                )
            )

        parts.append(b.markdown)
        cursor += len(b.markdown)
        if i < len(buckets) - 1:
            cursor += len(join_sep)

    content = join_sep.join(parts)
    metadata = {
        "source_format": "pdf",
        "parser": "mineru",
        "filename": file_path.name,
        "buckets": len(buckets),
        "page_count": page_count,
        "parse_time_seconds": sum(b.elapsed_s for b in buckets),
    }
    return ParserResult(content=content, images=images, metadata=metadata)


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
