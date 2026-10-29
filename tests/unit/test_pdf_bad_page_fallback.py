"""One page MinerU cannot parse must not cost the whole PDF.

Regression: a single zero-width OCR box raised ``ValueError("Unsupported
warp settings")`` inside MinerU 4.0.12 / docvortex, and the whole arXiv paper
it sat on ended up in error state, unsearchable.

Two layers are covered: the daemon-side crop guard that makes that box
harmless, and the parser-side containment that keeps any other per-page
MinerU error to the page it happened on.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import numpy as np
import pytest

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.services.parsers import _mineru_subprocess, pdf_parser
from voitta_rag_enterprise.services.parsers.mineru_result import (
    CONTENT_LIST_FILE,
    MARKDOWN_FILE,
)


def _make_pdf(path: Path, pages: int) -> Path:
    doc = fitz.open()
    for n in range(1, pages + 1):
        doc.new_page().insert_text((72, 72), f"text layer of page {n}")
    doc.save(path)
    doc.close()
    return path


class _StubDaemon:
    """Writes one MinerU-shaped block per page; raises on ``bad`` pages.

    ``error`` is what a range containing a bad page raises.
    """

    def __init__(
        self, pages: int, bad: set[int], error: type[Exception] = pdf_parser.MineruParseError
    ):
        self.pages = pages
        self.bad = bad
        self.error = error
        self.calls: list[str] = []

    def parse(self, *, pdf_path, page_range, out_dir, tier, parse_mode, timeout_s):
        self.calls.append(page_range)
        if page_range:
            lo, hi = (int(p) for p in page_range.split("-"))
        else:
            lo, hi = 1, self.pages
        if self.bad & set(range(lo, hi + 1)):
            raise self.error("MinerU error: ValueError('Unsupported warp settings')")
        out_dir.mkdir(parents=True)
        blocks = [
            {"type": "text", "text": f"MinerU markdown of page {n}", "page_idx": n - 1}
            for n in range(lo, hi + 1)
        ]
        (out_dir / MARKDOWN_FILE).write_text("\n\n".join(b["text"] for b in blocks))
        (out_dir / CONTENT_LIST_FILE).write_text(json.dumps(blocks))


@pytest.fixture
def stub(env: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOITTA_USE_FAKE_PDF_PARSER", "false")
    monkeypatch.setenv("VOITTA_PDF_RENDER_PAGES", "false")
    reset_settings_cache()
    monkeypatch.setattr(pdf_parser, "_log_gpu_once", lambda: None)

    def install(pages: int, bad: set[int], *, bucket: int = 20, **kw) -> _StubDaemon:
        daemon = _StubDaemon(pages, bad, **kw)
        monkeypatch.setattr(pdf_parser, "_mineru_daemon", lambda: daemon)
        monkeypatch.setattr(
            pdf_parser,
            "get_caps",
            lambda: SimpleNamespace(pdf_pages_per_bucket=bucket, pdf_parse_timeout_s=600),
        )
        return daemon

    return install


def _page_of(result, needle: str) -> int:
    offset = result.content.index(needle)
    return max(p for o, p in result.char_to_page if o <= offset)


def test_bad_page_falls_back_to_text_layer(stub, tmp_path: Path) -> None:
    daemon = stub(pages=3, bad={2})
    result = pdf_parser.PdfParser().parse(_make_pdf(tmp_path / "a.pdf", 3))

    assert result.success
    assert daemon.calls == ["", "1-1", "2-2", "3-3"]
    assert result.metadata["text_fallback_pages"] == [2]
    assert "MinerU markdown of page 1" in result.content
    assert "text layer of page 2" in result.content
    assert "MinerU markdown of page 3" in result.content
    # The fallback text is anchored to its own page, not the previous one.
    assert _page_of(result, "text layer of page 2") == 2
    assert _page_of(result, "MinerU markdown of page 3") == 3


def test_only_the_failing_bucket_is_split(stub, tmp_path: Path) -> None:
    daemon = stub(pages=5, bad={4}, bucket=2)
    result = pdf_parser.PdfParser().parse(_make_pdf(tmp_path / "a.pdf", 5))

    assert daemon.calls == ["1-2", "3-4", "3-3", "4-4", "5-5"]
    assert result.metadata["text_fallback_pages"] == [4]
    assert [_page_of(result, f"page {n}") for n in range(1, 6)] == [1, 2, 3, 4, 5]


def test_clean_document_is_unchanged(stub, tmp_path: Path) -> None:
    daemon = stub(pages=3, bad=set())
    result = pdf_parser.PdfParser().parse(_make_pdf(tmp_path / "a.pdf", 3))

    assert daemon.calls == [""]
    assert "text_fallback_pages" not in result.metadata


def test_every_page_failing_still_fails_the_file(stub, tmp_path: Path) -> None:
    stub(pages=2, bad={1, 2})
    with pytest.raises(pdf_parser.MineruParseError, match="Unsupported warp settings"):
        pdf_parser.PdfParser().parse(_make_pdf(tmp_path / "a.pdf", 2))


def test_timeouts_are_not_retried_per_page(stub, tmp_path: Path) -> None:
    daemon = stub(pages=3, bad={2}, error=TimeoutError)
    with pytest.raises(TimeoutError):
        pdf_parser.PdfParser().parse(_make_pdf(tmp_path / "a.pdf", 3))
    assert daemon.calls == [""]


# --- daemon-side crop guard (needs the real MinerU) -------------------------


@pytest.fixture
def ocr_image(monkeypatch: pytest.MonkeyPatch):
    module = pytest.importorskip("mineru.model.ocr.image")
    # Restore the unpatched helper and flag after each test.
    monkeypatch.setattr(module, "get_rotate_crop_image", module.get_rotate_crop_image)
    monkeypatch.setattr(_mineru_subprocess, "_CROP_GUARD_INSTALLED", False)
    return module


_IMG = np.full((100, 200, 3), 200, np.uint8)
# Quads whose short side collapses below one pixel — what OCR detection
# returned on the failing page.
_DEGENERATE = {
    "rotated": np.float32([[10, 10], [60, 40], [59.6, 40.5], [9.6, 10.5]]),
    "axis-aligned": np.float32([[10, 10], [10, 10], [10, 50], [10, 50]]),
}


@pytest.mark.parametrize("name", list(_DEGENERATE))
def test_degenerate_box_crashes_unguarded_mineru(ocr_image, name: str) -> None:
    # If this starts passing, MinerU fixed it upstream and the guard can go.
    with pytest.raises(ValueError, match="Unsupported warp settings"):
        ocr_image.get_rotate_crop_image_for_text_rec(_IMG, _DEGENERATE[name].copy())


@pytest.mark.parametrize("name", list(_DEGENERATE))
def test_guard_turns_degenerate_box_into_blank_crop(ocr_image, name: str) -> None:
    _mineru_subprocess._install_degenerate_crop_guard()
    crop = ocr_image.get_rotate_crop_image_for_text_rec(_IMG, _DEGENERATE[name].copy())

    assert crop is not None and min(crop.shape[:2]) == 1
    assert crop.ndim == 3 and crop.dtype == _IMG.dtype and (crop == 255).all()
    # The recognizer's preprocessing accepts it.
    ocr_image.resize_text_recognition_image(crop, 10.0, (3, 48, 320))


def test_guard_leaves_normal_boxes_and_other_errors_alone(
    ocr_image, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = np.float32([[10, 10], [60, 20], [58, 40], [8, 30]])
    expected = ocr_image.get_rotate_crop_image(_IMG, box.copy())
    _mineru_subprocess._install_degenerate_crop_guard()
    np.testing.assert_array_equal(ocr_image.get_rotate_crop_image(_IMG, box.copy()), expected)

    def broken(img, points):
        raise ValueError("Non-finite affine points")

    monkeypatch.setattr(ocr_image, "get_rotate_crop_image", broken)
    monkeypatch.setattr(_mineru_subprocess, "_CROP_GUARD_INSTALLED", False)
    _mineru_subprocess._install_degenerate_crop_guard()
    with pytest.raises(ValueError, match="Non-finite"):
        ocr_image.get_rotate_crop_image(_IMG, box.copy())
