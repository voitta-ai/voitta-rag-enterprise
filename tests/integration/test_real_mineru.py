"""PdfParser against the real MinerU 4 daemon — opt-in, not run in CI.

Needs MinerU 4 and its basic-tier models (downloaded on first use). Run on a
GPU box or a Mac after any MinerU / torch / transformers change:

    VOITTA_REAL_MINERU=1 pytest tests/integration/test_real_mineru.py
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.services import indexing_caps
from voitta_rag_enterprise.services.parsers import pdf_parser
from voitta_rag_enterprise.services.parsers.base import UnsupportedDocumentError

pytestmark = pytest.mark.skipif(
    os.environ.get("VOITTA_REAL_MINERU") != "1" or importlib.util.find_spec("mineru") is None,
    reason="real MinerU run is opt-in: set VOITTA_REAL_MINERU=1",
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mineru4"


@pytest.fixture
def real_parser(env: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VOITTA_USE_FAKE_PDF_PARSER", raising=False)
    reset_settings_cache()
    pdf_parser.reset_mineru_daemon_for_tests()
    yield pdf_parser.PdfParser()
    pdf_parser.reset_mineru_daemon_for_tests()


def _bucket_size(monkeypatch: pytest.MonkeyPatch, pages: int) -> None:
    caps = indexing_caps.IndexingCaps(pdf_pages_per_bucket=pages)
    monkeypatch.setattr(pdf_parser, "get_caps", lambda: caps)


def test_single_parse_end_to_end(real_parser: pdf_parser.PdfParser) -> None:
    result = real_parser.parse(FIXTURES / "sample.pdf")
    assert result.content.startswith("## Annual Operating Review")
    assert "| Hardware | 412.5 | 18.2 |" in result.content
    assert [(i.page, i.mime) for i in result.images] == [(1, "image/jpeg")]
    assert [p.page for p in result.page_images] == [1, 2, 3]
    assert {b["page"] for b in result.page_layout} == {1, 2, 3}
    assert [p for _, p in result.char_to_page] == [1, 2, 3]
    assert result.metadata["buckets"] == 1


def test_bucketed_parse_matches_single_parse(
    real_parser: pdf_parser.PdfParser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One page per bucket (3 page_range requests) must yield the same pages,
    images and layout as one whole-document request."""
    single = real_parser.parse(FIXTURES / "sample.pdf")
    _bucket_size(monkeypatch, 1)
    bucketed = real_parser.parse(FIXTURES / "sample.pdf")
    assert bucketed.metadata["buckets"] == 3
    norm = lambda s: " ".join(s.split())  # noqa: E731 — joiners differ
    assert norm(bucketed.content) == norm(single.content)
    assert [i.page for i in bucketed.images] == [i.page for i in single.images]
    assert [p.page for p in bucketed.page_images] == [1, 2, 3]
    assert [(b["page"], b["type"]) for b in bucketed.page_layout] == [
        (b["page"], b["type"]) for b in single.page_layout
    ]
    assert [p for _, p in bucketed.char_to_page] == [1, 2, 3]


def test_password_protected_pdf_is_unsupported(real_parser: pdf_parser.PdfParser) -> None:
    with pytest.raises(UnsupportedDocumentError, match="password-protected"):
        real_parser.parse(FIXTURES / "encrypted.pdf")
    # The daemon survives a per-file error and parses the next file.
    assert real_parser.parse(FIXTURES / "sample.pdf").content


def test_truncated_pdf_is_repaired_or_a_coded_error(
    real_parser: pdf_parser.PdfParser, tmp_path: Path
) -> None:
    """PDFium repairs many truncated files; when it cannot, the failure is a
    MinerU error. Either way the daemon keeps serving the next file."""
    src = (FIXTURES / "sample.pdf").read_bytes()
    bad = tmp_path / "truncated.pdf"
    bad.write_bytes(src[: len(src) // 3])
    try:
        real_parser.parse(bad)
    except RuntimeError as e:
        assert "MinerU error" in str(e)
    assert real_parser.parse(FIXTURES / "sample.pdf").content


def test_error_classifier_maps_pdfium_load_errors() -> None:
    from mineru.errors import InvalidRequestError
    from pypdfium2 import PdfiumError

    from voitta_rag_enterprise.services.parsers import _mineru_subprocess as sub

    assert sub._error_code(PdfiumError("x", err_code=4)) == "file_encrypted"
    assert sub._error_code(PdfiumError("x", err_code=3)) == "file_corrupted"
    assert sub._error_code(PdfiumError("x", err_code=None)) is None
    assert sub._error_code(InvalidRequestError("page_range_invalid", "bad", "page_range")) == "page_range_invalid"
    assert sub._error_code(ValueError("x")) is None


def _repeat_sample(tmp_path: Path, copies: int) -> Path:
    import fitz

    out = tmp_path / f"sample_x{copies}.pdf"
    doc = fitz.open()
    src = fitz.open(FIXTURES / "sample.pdf")
    for _ in range(copies):
        doc.insert_pdf(src)
    doc.save(out)
    return out


def test_daemon_runs_one_render_process(
    real_parser: pdf_parser.PdfParser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MinerU starts one render process per 30 pages (up to 3). A 96-page
    request would get 3 by default; MINERU_PDF_RENDER_THREADS=1 must hold it
    to exactly 1 for the whole parse."""
    import threading

    psutil = pytest.importorskip("psutil")
    big = _repeat_sample(tmp_path, 32)  # 96 pages
    _bucket_size(monkeypatch, 200)  # one request for the whole file
    peak = [0]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.05):
            daemon = pdf_parser._DAEMON
            proc = daemon._proc if daemon is not None else None
            if proc is None or proc.poll() is not None:
                continue
            try:
                kids = psutil.Process(proc.pid).children(recursive=True)
                workers = [k for k in kids if "spawn_main" in " ".join(k.cmdline())]
            except psutil.Error:
                continue
            peak[0] = max(peak[0], len(workers))

    t = threading.Thread(target=sample, daemon=True)
    t.start()
    try:
        assert real_parser.parse(big).metadata["page_count"] == 96
    finally:
        stop.set()
        t.join()
    assert peak[0] == 1


def test_daemon_kill_mid_parse_respawns(
    real_parser: pdf_parser.PdfParser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The watchdog SIGKILLs a parse that overruns; the next parse respawns
    a fresh daemon and succeeds."""
    big = _repeat_sample(tmp_path, 40)
    caps = [indexing_caps.IndexingCaps(pdf_parse_timeout_s=1, pdf_pages_per_bucket=200)]
    monkeypatch.setattr(pdf_parser, "get_caps", lambda: caps[0])
    with pytest.raises(TimeoutError, match="exceeded 1s"):
        real_parser.parse(big)
    caps[0] = indexing_caps.IndexingCaps()
    assert real_parser.parse(FIXTURES / "sample.pdf").content


def test_killed_daemon_leaves_no_render_workers(
    real_parser: pdf_parser.PdfParser, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A watchdog kill takes the render workers down with the daemon (they
    share its process group) and returns promptly."""
    import time

    psutil = pytest.importorskip("psutil")
    big = _repeat_sample(tmp_path, 40)
    # An explicit render timeout keeps the budget switch below from
    # respawning the daemon, so the kill hits a warm daemon mid-render.
    monkeypatch.setenv("MINERU_PDF_RENDER_TIMEOUT", "300")
    caps = [indexing_caps.IndexingCaps(pdf_pages_per_bucket=200)]
    monkeypatch.setattr(pdf_parser, "get_caps", lambda: caps[0])
    real_parser.parse(FIXTURES / "sample.pdf")  # warm daemon, render worker started
    caps[0] = indexing_caps.IndexingCaps(pdf_parse_timeout_s=3, pdf_pages_per_bucket=200)
    pgid = pdf_parser._DAEMON._proc.pid
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        real_parser.parse(big)
    assert time.monotonic() - started < 3 + 10
    time.sleep(0.5)
    leftovers = [p for p in psutil.process_iter(["pid"]) if _pgid(p.pid) == pgid]
    assert leftovers == []


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def test_fixture_dir_is_untouched() -> None:
    """Parsing writes only to temp dirs, never next to the source PDF."""
    names = {p.name for p in FIXTURES.iterdir()}
    assert names <= {
        "sample.pdf", "encrypted.pdf", "full", "range_2_3",
        "make_fixture_pdf.py", "record_fixture.py", "__pycache__",
    }
