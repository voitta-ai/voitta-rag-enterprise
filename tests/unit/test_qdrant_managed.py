"""Managed Qdrant subprocess launcher — start/health/teardown + hard-fail.

The launcher's whole contract is "no fallback": a missing or unhealthy binary
must RAISE, never silently degrade to the embedded backend. On top of that the
lifecycle is hardened: orphan sweep via pidfile, no adoption of a foreign
Qdrant (busy fixed port is a hard error; readiness requires OUR child alive),
per-boot API key, child output captured to qdrant.log, and a watchdog that
hard-fails the app if the sidecar dies mid-run. These tests prove all of it
with fake binaries (a tiny HTTP server stands in for a healthy Qdrant), so no
real qdrant binary is needed.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

from voitta_rag_enterprise.config import Settings
from voitta_rag_enterprise.services import qdrant_process as qp


def _write_exe(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


# A fake "qdrant" that serves 200 on /readyz at the env-configured HTTP port —
# but only when the request carries the env-configured API key, like the real
# server does. Proves the readiness probe authenticates.
_HEALTHY_SERVER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import http.server, os
    port = int(os.environ["QDRANT__SERVICE__HTTP_PORT"])
    key = os.environ.get("QDRANT__SERVICE__API_KEY", "")
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if key and self.headers.get("api-key") != key:
                self.send_response(401)
            else:
                self.send_response(200 if self.path == "/readyz" else 404)
            self.end_headers()
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
""")


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


def _managed_dir(tmp_path: Path) -> Path:
    return tmp_path / "qdrant_managed"


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


def test_early_exit_error_includes_log_tail(tmp_path, monkeypatch):
    # Whatever the child printed before dying must surface in the raise — a
    # startup panic has to be self-explanatory from the app log alone.
    fake = _write_exe(
        tmp_path / "qdrant_panic", "#!/bin/sh\necho 'boom: storage locked'\nexit 101\n"
    )
    s = _settings(tmp_path, fake, qdrant_managed_startup_timeout_s=5.0)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError, match="boom: storage locked"):
        qp.start_managed_qdrant()


def test_never_ready_times_out(tmp_path, monkeypatch):
    # Sleeps forever, never opens a port → must hit the readiness timeout.
    fake = _write_exe(tmp_path / "qdrant_hang", "#!/bin/sh\nsleep 30\n")
    s = _settings(tmp_path, fake, qdrant_managed_startup_timeout_s=2.0)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    with pytest.raises(RuntimeError, match="not ready within"):
        qp.start_managed_qdrant()


def test_healthy_binary_starts_and_stops(tmp_path, monkeypatch):
    # Happy path with a pinned port: resolve → sweep → spawn → poll ready →
    # return URL. Pidfile exists while running, gone after stop.
    port = 6399
    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake, qdrant_managed_http_port=port)
    monkeypatch.setattr(qp, "get_settings", lambda: s)

    url = qp.start_managed_qdrant()
    assert url == f"http://127.0.0.1:{port}"
    # Idempotent: second call returns same URL without spawning a new process.
    assert qp.start_managed_qdrant() == url

    pid_path = _managed_dir(tmp_path) / "qdrant.pid"
    info = json.loads(pid_path.read_text())
    assert info["binary"] == fake
    assert qp._pid_alive(info["pid"])
    assert qp.managed_qdrant_api_key()  # available while running

    qp.stop_managed_qdrant()
    assert not pid_path.exists()
    with pytest.raises(RuntimeError, match="not running"):
        qp.managed_qdrant_api_key()
    # After stop, a fresh start spawns again (no lingering handle).
    assert qp.start_managed_qdrant() == url


def test_child_cwd_is_storage_dir(tmp_path, monkeypatch):
    # Qdrant resolves some paths (./snapshots/tmp) relative to its CWD even
    # with STORAGE_PATH set; from a .app bundle the inherited CWD is read-only
    # and the real binary panics at boot. The child must be anchored in the
    # storage dir.
    recorder = textwrap.dedent("""\
        #!/usr/bin/env python3
        import http.server, os, pathlib
        pathlib.Path("cwd.txt").write_text(os.getcwd())
        port = int(os.environ["QDRANT__SERVICE__HTTP_PORT"])
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/readyz" else 404)
                self.end_headers()
            def log_message(self, *a): pass
        http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
    """)
    fake = _write_exe(tmp_path / "qdrant_cwd", recorder)
    s = _settings(tmp_path, fake)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    qp.start_managed_qdrant()

    managed = _managed_dir(tmp_path)
    assert (managed / "cwd.txt").read_text() == str(managed)


def test_auto_port_picks_a_free_one(tmp_path, monkeypatch):
    # Default ports are 0 → the launcher picks free ephemeral ports itself.
    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake)  # http/grpc ports default to 0
    monkeypatch.setattr(qp, "get_settings", lambda: s)

    url = qp.start_managed_qdrant()
    port = urlparse(url).port
    assert port and port > 0


def test_fixed_port_busy_refuses_to_adopt(tmp_path, monkeypatch):
    # Something else already listens on the pinned port → hard error, the
    # launcher must never treat a foreign listener as its own sidecar.
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    busy_port = squatter.getsockname()[1]
    try:
        fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
        s = _settings(tmp_path, fake, qdrant_managed_http_port=busy_port)
        monkeypatch.setattr(qp, "get_settings", lambda: s)
        with pytest.raises(RuntimeError, match="refusing to adopt"):
            qp.start_managed_qdrant()
    finally:
        squatter.close()


def test_orphan_sweep_kills_previous_child(tmp_path, monkeypatch):
    # Simulate a dirty death: a pidfile from a "previous run" points at a
    # still-running process launched from the same binary path. Boot must
    # kill it before spawning fresh.
    orphan_bin = _write_exe(tmp_path / "qdrant_orphan", "#!/bin/sh\nsleep 60\n")
    orphan = subprocess.Popen([orphan_bin])
    try:
        managed = _managed_dir(tmp_path)
        managed.mkdir(parents=True)
        (managed / "qdrant.pid").write_text(
            json.dumps({"pid": orphan.pid, "binary": orphan_bin})
        )

        fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
        s = _settings(tmp_path, fake)
        monkeypatch.setattr(qp, "get_settings", lambda: s)
        qp.start_managed_qdrant()

        assert orphan.wait(timeout=5) is not None  # swept
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_recycled_pid_is_not_killed(tmp_path, monkeypatch):
    # Pidfile points at a live pid whose command is NOT our binary (the pid
    # was recycled by the OS) → remove the stale pidfile, kill nothing.
    managed = _managed_dir(tmp_path)
    managed.mkdir(parents=True)
    pid_path = managed / "qdrant.pid"
    pid_path.write_text(
        json.dumps({"pid": os.getpid(), "binary": "/some/old/install/qdrant"})
    )

    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    qp.start_managed_qdrant()  # would SIGTERM the test runner if this is wrong

    info = json.loads(pid_path.read_text())
    assert info["pid"] != os.getpid()  # pidfile now describes the new child


def test_dead_pid_pidfile_is_cleaned(tmp_path, monkeypatch):
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    managed = _managed_dir(tmp_path)
    managed.mkdir(parents=True)
    (managed / "qdrant.pid").write_text(
        json.dumps({"pid": dead.pid, "binary": "/usr/bin/true"})
    )

    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    qp.start_managed_qdrant()  # must not raise over the stale entry


def test_watchdog_fires_on_unexpected_death(tmp_path, monkeypatch):
    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    died = threading.Event()
    monkeypatch.setattr(qp, "_die", died.set)

    qp.start_managed_qdrant()
    qp._proc.kill()  # sidecar dies behind the app's back

    assert died.wait(timeout=5), "watchdog did not hard-fail on sidecar death"


def test_watchdog_quiet_on_requested_stop(tmp_path, monkeypatch):
    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    died = threading.Event()
    monkeypatch.setattr(qp, "_die", died.set)

    qp.start_managed_qdrant()
    qp.stop_managed_qdrant()

    time.sleep(1.0)  # give a buggy watchdog the chance to misfire
    assert not died.is_set()


def test_watchdog_quiet_across_stop_start_cycle(tmp_path, monkeypatch):
    # The first child's watchdog must not fire after a stop→start cycle has
    # reset the stopping flag (it must key off "is my proc still current").
    fake = _write_exe(tmp_path / "qdrant_ok", _HEALTHY_SERVER)
    s = _settings(tmp_path, fake)
    monkeypatch.setattr(qp, "get_settings", lambda: s)
    died = threading.Event()
    monkeypatch.setattr(qp, "_die", died.set)

    qp.start_managed_qdrant()
    qp.stop_managed_qdrant()
    qp.start_managed_qdrant()

    time.sleep(1.0)
    assert not died.is_set()
