"""Managed Qdrant subprocess launcher — start/health/teardown + hard-fail.

The launcher's whole contract is "no fallback": a missing or unhealthy binary
must RAISE, never silently degrade to the embedded backend. These tests prove
that with fake binaries (a tiny HTTP server stands in for a healthy Qdrant), so
no real qdrant binary is needed.
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from voitta_rag_enterprise.config import Settings
from voitta_rag_enterprise.services import qdrant_process as qp


def _write_exe(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


@pytest.fixture(autouse=True)
def _stop_after():
    yield
    qp.stop_managed_qdrant()


def _settings(tmp_path: Path, binary: str, **over) -> Settings:
    over.setdefault("qdrant_managed_startup_timeout_s", 8.0)
    return Settings(
        data_dir=tmp_path,
        qdrant_mode="managed",
        qdrant_binary=binary,
        **over,
    )


def test_missing_binary_raises(tmp_path, monkeypatch):
    s = _settings(tmp_path, "/nonexistent/qdrant-xyz")
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError, match="not found or not executable"):
        qp.start_managed_qdrant()


def test_missing_on_path_raises(tmp_path, monkeypatch):
    s = _settings(tmp_path, "definitely-not-a-real-binary-name-xyz")
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError, match="not found on PATH"):
        qp.start_managed_qdrant()


def test_early_exit_raises(tmp_path, monkeypatch):
    fake = _write_exe(tmp_path / "qdrant_exit", "#!/bin/sh\nexit 3\n")
    s = _settings(tmp_path, fake, qdrant_managed_startup_timeout_s=5.0)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError, match=r"exited .*before becoming ready"):
        qp.start_managed_qdrant()


def test_never_ready_times_out(tmp_path, monkeypatch):
    # Sleeps forever, never opens a port → must hit the readiness timeout.
    fake = _write_exe(tmp_path / "qdrant_hang", "#!/bin/sh\nsleep 30\n")
    s = _settings(tmp_path, fake, qdrant_managed_startup_timeout_s=2.0)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError, match="not ready within"):
        qp.start_managed_qdrant()


def test_healthy_binary_starts_and_stops(tmp_path, monkeypatch):
    # A fake "qdrant" that serves 200 on /readyz at the configured HTTP port,
    # proving the happy path: resolve → spawn → poll ready → return URL.
    port = 6399
    server = textwrap.dedent("""\
        #!/usr/bin/env python3
        import http.server, os
        port = int(os.environ["QDRANT__SERVICE__HTTP_PORT"])
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/readyz" else 404)
                self.end_headers()
            def log_message(self, *a): pass
        http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
    """)
    fake = _write_exe(tmp_path / "qdrant_ok", server)
    s = _settings(tmp_path, fake, qdrant_managed_http_port=port)
    monkeypatch.setattr(qp, "get_settings", lambda: s)

    url = qp.start_managed_qdrant()
    assert url == f"http://127.0.0.1:{port}"
    # Idempotent: second call returns same URL without spawning a new process.
    assert qp.start_managed_qdrant() == url

    qp.stop_managed_qdrant()
    # After stop, a fresh start spawns again (no lingering handle).
    assert qp.start_managed_qdrant() == url
