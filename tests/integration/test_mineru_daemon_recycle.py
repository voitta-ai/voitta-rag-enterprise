"""The MinerU daemon must not grow until the OS kills it mid-parse.

Regression: on a 36 GB Mac the long-lived daemon's footprint grew with every
page parsed and was never returned; after roughly 4,800 pages jetsam
SIGKILLed it (rc=-9), every time, and the file it happened to be on failed.

Covered here, against a stub subprocess (as in ``test_mineru_daemon``):
the daemon is recycled between requests on a page budget or on footprint
growth, and a worker death mid-parse surfaces as ``MineruWorkerDied`` so the
parser retries the range once on a fresh daemon.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.services.parsers import pdf_parser
from voitta_rag_enterprise.services.parsers.pdf_parser import (
    MineruWorkerDied,
    reset_mineru_daemon_for_tests,
)

GB = 2**30

_OK_STUB = """
import json, sys
for line in sys.stdin:
    sys.stdout.write(json.dumps({"status": "ok"}) + "\\n")
    sys.stdout.flush()
"""

# SIGKILLs itself on the first request it ever sees (the marker file
# outlives the process), answers normally after a respawn.
_DIE_ONCE_STUB = """
import json, os, signal, sys
marker = sys.argv[1]
for line in sys.stdin:
    if not os.path.exists(marker):
        open(marker, "w").close()
        os.kill(os.getpid(), signal.SIGKILL)
    sys.stdout.write(json.dumps({"status": "ok"}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture(autouse=True)
def _reset_daemon(env: None, monkeypatch: pytest.MonkeyPatch):
    reset_mineru_daemon_for_tests()
    yield
    reset_mineru_daemon_for_tests()


def _settings(monkeypatch: pytest.MonkeyPatch, *, pages: int = 0, growth_gb: float = 0) -> None:
    monkeypatch.setenv("VOITTA_MINERU_RECYCLE_PAGES", str(pages))
    monkeypatch.setenv("VOITTA_MINERU_RECYCLE_GROWTH_GB", str(growth_gb))
    reset_settings_cache()


def _stub_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, *args: str):
    """The process-wide daemon, spawning ``body`` instead of real MinerU.

    Returns it and the list of pids it spawned, in order.
    """
    stub = tmp_path / "stub.py"
    stub.write_text(textwrap.dedent(body))
    daemon = pdf_parser._mineru_daemon()
    pids: list[int] = []

    def fake_spawn() -> None:
        daemon._proc = subprocess.Popen(
            [sys.executable, str(stub), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            start_new_session=True,
        )
        daemon._stderr_thread = threading.Thread(target=daemon._drain_stderr, args=(daemon._proc.stderr,), daemon=True)
        daemon._stderr_thread.start()
        pids.append(daemon._proc.pid)

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    return daemon, pids


def _parse(daemon, tmp_path: Path, *, pages: int) -> None:
    daemon.parse(
        pdf_path=Path("/some/file.pdf"),
        page_range="",
        out_dir=tmp_path / "out",
        tier="basic",
        parse_mode="auto",
        timeout_s=10,
        pages=pages,
    )


def test_page_budget_recycles_between_requests(tmp_path, monkeypatch) -> None:
    _settings(monkeypatch, pages=10)
    daemon, pids = _stub_daemon(monkeypatch, tmp_path, _OK_STUB)

    _parse(daemon, tmp_path, pages=6)
    _parse(daemon, tmp_path, pages=6)  # 12 >= 10: retired after answering
    assert daemon._proc is None
    _parse(daemon, tmp_path, pages=6)  # fresh daemon, budget counts from zero
    _parse(daemon, tmp_path, pages=3)
    assert len(pids) == 2 and daemon._proc is not None


def test_footprint_growth_recycles(tmp_path, monkeypatch) -> None:
    _settings(monkeypatch, growth_gb=4)
    readings = iter([10 * GB, 12 * GB, 14.5 * GB, 10 * GB])
    monkeypatch.setattr(pdf_parser, "_process_memory_bytes", lambda pid: next(readings))
    monkeypatch.setattr(pdf_parser, "_RECYCLE_MIN_PAGES", 0)
    daemon, pids = _stub_daemon(monkeypatch, tmp_path, _OK_STUB)

    _parse(daemon, tmp_path, pages=5)  # baseline 10 GB
    _parse(daemon, tmp_path, pages=5)  # +2 GB: keep
    assert daemon._proc is not None
    _parse(daemon, tmp_path, pages=5)  # +4.5 GB: recycle
    assert daemon._proc is None
    _parse(daemon, tmp_path, pages=5)  # new baseline
    assert len(pids) == 2


def test_growth_needs_a_minimum_of_pages(tmp_path, monkeypatch) -> None:
    """Models load lazily on the pages that first need them, so a jump right
    after spawn is not a leak and must not trigger a respawn loop."""
    _settings(monkeypatch, growth_gb=4)
    readings = iter([5 * GB, 11 * GB])
    monkeypatch.setattr(pdf_parser, "_process_memory_bytes", lambda pid: next(readings))
    daemon, pids = _stub_daemon(monkeypatch, tmp_path, _OK_STUB)

    _parse(daemon, tmp_path, pages=2)
    _parse(daemon, tmp_path, pages=2)
    assert daemon._proc is not None and len(pids) == 1


def test_disabled_recycling_keeps_the_daemon(tmp_path, monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(pdf_parser, "_process_memory_bytes", lambda pid: 100 * GB)
    monkeypatch.setattr(pdf_parser, "_RECYCLE_MIN_PAGES", 0)
    daemon, pids = _stub_daemon(monkeypatch, tmp_path, _OK_STUB)

    for _ in range(3):
        _parse(daemon, tmp_path, pages=1000)
    assert len(pids) == 1


def test_worker_death_is_typed_and_the_next_request_respawns(tmp_path, monkeypatch) -> None:
    _settings(monkeypatch)
    daemon, pids = _stub_daemon(monkeypatch, tmp_path, _DIE_ONCE_STUB, str(tmp_path / "died"))

    with pytest.raises(MineruWorkerDied, match=r"rc=-9.*OOM"):
        _parse(daemon, tmp_path, pages=1)
    _parse(daemon, tmp_path, pages=1)
    assert len(pids) == 2


def test_memory_reading_works_on_this_platform() -> None:
    if sys.platform not in ("darwin", "linux"):
        pytest.skip("only macOS and Linux are measured")
    used = pdf_parser._process_memory_bytes(os.getpid())
    assert used is not None and 1 * 2**20 < used < 1024 * GB
    assert pdf_parser._process_memory_bytes(2**22 + 12345) is None  # no such pid
