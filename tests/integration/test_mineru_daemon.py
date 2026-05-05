"""Tests for the MinerU subprocess daemon — focused on the timeout path.

We don't run real MinerU in CI (it pulls multiple GPU models). Instead we
swap the subprocess command for a small stub script that lets us drive
exactly the scenarios the daemon must handle: a healthy round-trip, a
timeout that triggers the watchdog kill, an error response, and recovery
after a kill (next call respawns and works).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from voitta_rag_enterprise.services.parsers import pdf_parser
from voitta_rag_enterprise.services.parsers.pdf_parser import (
    _MineruDaemon,
    reset_mineru_daemon_for_tests,
)

_STUB_COUNTER = 0


def _write_stub(tmp_path: Path, body: str) -> Path:
    """Write a tiny Python script that acts as the MinerU subprocess.

    The body is appended to a fixed prologue that drains stdin one line
    at a time and dispatches to ``handle(req)`` — keeps test stubs short.

    Each call gets a unique filename so a single test can write multiple
    stubs (e.g. "first hangs, second works") without overwriting earlier
    versions.
    """
    global _STUB_COUNTER
    _STUB_COUNTER += 1
    src = tmp_path / f"stub_{_STUB_COUNTER}.py"
    src.write_text(
        textwrap.dedent(
            """
            import json, sys, time
            """
        )
        + textwrap.dedent(body)
        + textwrap.dedent(
            """
            for line in sys.stdin:
                req = json.loads(line)
                resp = handle(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp) + '\\n')
                    sys.stdout.flush()
            """
        )
    )
    return src


@pytest.fixture(autouse=True)
def _reset_daemon():
    """Each test gets a fresh daemon — keeps watchdog state from leaking."""
    reset_mineru_daemon_for_tests()
    yield
    reset_mineru_daemon_for_tests()


def _patch_daemon(monkeypatch: pytest.MonkeyPatch, stub: Path) -> _MineruDaemon:
    """Replace the daemon's spawn command with our stub script."""
    daemon = pdf_parser._mineru_daemon()
    real_spawn = daemon._spawn

    def fake_spawn() -> None:
        # Re-call ``_spawn`` via the underlying Popen path but with our
        # stub script in place of the real subprocess module entry. Using
        # monkeypatch on subprocess.Popen would be too invasive; we just
        # rebind the daemon's ``_spawn``.
        import subprocess
        import threading

        daemon._proc = subprocess.Popen(
            [sys.executable, str(stub)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
        )
        daemon._stderr_thread = threading.Thread(
            target=daemon._drain_stderr,
            args=(daemon._proc.stderr,),
            daemon=True,
        )
        daemon._stderr_thread.start()

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    # Suppress the real spawn function so it never gets called.
    _ = real_spawn  # silence unused
    return daemon


def test_healthy_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _write_stub(tmp_path, "def handle(req): return {'status': 'ok'}")
    daemon = _patch_daemon(monkeypatch, stub)
    daemon.parse(
        bucket_path=Path("/some/file.pdf"),
        out_root=tmp_path / "out",
        method="auto",
        lang="en",
        pdf_name="file",
        timeout_s=5,
    )
    # Implicitly: no exception raised.


def test_error_response_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _write_stub(
        tmp_path,
        "def handle(req): return {'status': 'error', 'detail': 'boom', 'traceback': 'trace'}",
    )
    daemon = _patch_daemon(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="MinerU error: boom"):
        daemon.parse(
            bucket_path=Path("/some/file.pdf"),
            out_root=tmp_path / "out",
            method="auto",
            lang="en",
            pdf_name="file",
            timeout_s=5,
        )


def test_timeout_kills_subprocess_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub sleeps forever; the watchdog should kill it and surface
    TimeoutError to the caller. extract's ``except Exception`` then routes
    that into ``_mark_error`` (state='error'), which is the whole point."""
    stub = _write_stub(
        tmp_path,
        # Deliberately never returns — emulates a wedged MinerU call.
        "def handle(req):\n    time.sleep(60)\n    return {'status': 'ok'}",
    )
    daemon = _patch_daemon(monkeypatch, stub)
    with pytest.raises(TimeoutError, match="exceeded 1s"):
        daemon.parse(
            bucket_path=Path("/some/file.pdf"),
            out_root=tmp_path / "out",
            method="auto",
            lang="en",
            pdf_name="file",
            timeout_s=1,
        )
    # The subprocess must actually be dead now — otherwise we'd leak a
    # process per timeout.
    assert daemon._proc is not None
    daemon._proc.wait(timeout=2)
    assert daemon._proc.poll() is not None


def test_subsequent_call_respawns_after_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a timeout kill, the next ``parse`` must spawn a fresh
    subprocess and complete successfully — the queue keeps moving past
    one bad PDF, which is the whole reason this exists."""
    # First stub: hangs. Second stub: works. We swap by writing two files
    # and rebinding the daemon's _spawn target between calls.
    bad = _write_stub(tmp_path, "def handle(req):\n    time.sleep(60)\n    return None")
    good = _write_stub(tmp_path, "def handle(req): return {'status': 'ok'}")

    daemon = _patch_daemon(monkeypatch, bad)
    with pytest.raises(TimeoutError):
        daemon.parse(
            bucket_path=Path("/x.pdf"),
            out_root=tmp_path / "out",
            method="auto",
            lang="en",
            pdf_name="x",
            timeout_s=1,
        )

    # Re-patch with the good stub — emulates "next iteration of the worker
    # loop, fresh subprocess will load successfully".
    _patch_daemon(monkeypatch, good)
    daemon.parse(
        bucket_path=Path("/y.pdf"),
        out_root=tmp_path / "out",
        method="auto",
        lang="en",
        pdf_name="y",
        timeout_s=5,
    )


def test_settings_carry_default_timeout() -> None:
    """Sanity: the new env var has a sensible default and is wired through."""
    from voitta_rag_enterprise.config import get_settings

    assert get_settings().pdf_parse_timeout_s == 600
