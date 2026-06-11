"""Managed Qdrant subprocess launcher (``qdrant_mode="managed"``).

Spawns the native Qdrant binary as a localhost child process and waits for it
to report healthy; the vector store then connects to it over HTTP. This gives
full-engine feature parity with **no Docker / container runtime** — the binary
is the same Rust server the Docker image wraps.

**Hard-fail by design.** If the binary can't be found, fails to launch, exits
early, or doesn't become ready within the timeout, every function here
*raises*. There is deliberately NO fallback to the embedded backend: a
misconfigured ``managed`` deployment must crash at boot, not silently degrade
to a different (stripped-down) engine. The launcher is wired into the
synchronous part of the app lifespan so that raise aborts startup. The same
philosophy extends past boot: a watchdog thread hard-fails the whole app if
the sidecar dies mid-run, instead of serving with a dead vector store.

Lifecycle guarantees (cradle to grave):

* **Orphan sweep** — a pidfile in the storage dir records the child we
  spawned. A dirty death of the app (SIGKILL, crash) can orphan the child;
  the next boot finds the pidfile, verifies the pid still runs *our* binary,
  and kills it before spawning fresh. No Qdrant ever survives past the next
  launch.
* **No foreign adoption** — ports default to 0 (pick a free ephemeral port at
  spawn); a fixed configured port is pre-flight checked and a busy port is a
  hard error, never "someone is already serving there, great". Additionally a
  per-boot random API key is passed to the child and required on every probe
  and client call, so even a port race cannot make the app adopt a Qdrant it
  doesn't own.
* **Diagnosable child** — stdout/stderr go to ``qdrant.log`` in the storage
  dir (truncated each boot), and failure paths include its tail in the raised
  error / CRITICAL log, so a startup panic is self-explanatory from app logs.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import get_settings

logger = logging.getLogger(__name__)

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_stopping = False  # set by stop_managed_qdrant() so the watchdog stays quiet
_url: str | None = None
_api_key: str | None = None
_log_path: Path | None = None
_pid_path: Path | None = None

_LOG_TAIL_LINES = 30


def _default_die() -> None:
    """Hard-fail the whole app: SIGTERM to self. Under uvicorn this triggers
    a graceful shutdown; the desktop process exits with the cause already
    logged at CRITICAL by the watchdog."""
    os.kill(os.getpid(), signal.SIGTERM)


# Injectable so tests can assert the watchdog fired without killing the runner.
_die = _default_die


def managed_qdrant_api_key() -> str:
    """The per-boot API key the running sidecar requires on every call."""
    if _api_key is None:
        raise RuntimeError("managed Qdrant is not running — no API key")
    return _api_key


def _resolve_binary(qdrant_binary: str | None) -> str:
    """Resolve the Qdrant binary path, or raise (no fallback)."""
    cand = qdrant_binary or "qdrant"
    looks_like_path = os.sep in cand or (os.altsep is not None and os.altsep in cand)
    if looks_like_path:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        raise RuntimeError(
            f"qdrant_mode=managed: binary not found or not executable: {cand!r}"
        )
    found = shutil.which(cand)
    if not found:
        raise RuntimeError(
            f"qdrant_mode=managed: '{cand}' not found on PATH. Install the "
            "Qdrant binary, or set VOITTA_QDRANT_BINARY to its absolute path."
        )
    return found


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    return True


def _pid_command(pid: int) -> str:
    """The full command line of ``pid`` via ``ps`` (portable macOS/Linux);
    empty string if the process is gone or ps fails."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _sweep_orphan(pid_path: Path) -> None:
    """Kill a Qdrant child orphaned by a dirty death of a previous run.

    The pidfile records the pid *and* the binary path it was launched as; we
    only kill when the live pid's command still matches that binary, so a
    recycled pid is never killed — its stale pidfile is just removed.
    """
    try:
        info = json.loads(pid_path.read_text())
        pid = int(info["pid"])
        binary = str(info.get("binary", ""))
    except (OSError, ValueError, KeyError, TypeError):
        with contextlib.suppress(OSError):
            pid_path.unlink()
        return

    if _pid_alive(pid):
        cmd = _pid_command(pid)
        # Match strictly on the full recorded binary path (we always record
        # the absolute path we launched). A looser basename match can hit an
        # unrelated process whose command line merely mentions "qdrant".
        if binary and binary in cmd:
            logger.warning(
                "sweeping orphaned managed Qdrant from a previous run "
                "(pid=%d, binary=%s)", pid, binary,
            )
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.2)
            if _pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
                time.sleep(0.5)
        else:
            logger.info(
                "stale qdrant pidfile points at pid %d which is not our "
                "binary (recycled pid) — removing pidfile only", pid,
            )
    with contextlib.suppress(OSError):
        pid_path.unlink()


def _pick_port(host: str, configured: int, label: str) -> int:
    """Pick the port to bind: configured (pre-flight checked) or a free one.

    A busy fixed port is a hard error — adopting whatever already listens
    there is exactly the failure mode this module exists to prevent.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if configured:
            try:
                s.bind((host, configured))
            except OSError as e:
                raise RuntimeError(
                    f"qdrant_mode=managed: {label} port {configured} on {host} "
                    "is already in use by another process — refusing to adopt "
                    "it. Stop that process, or set the port to 0 (auto-pick)."
                ) from e
            return configured
        s.bind((host, 0))
        return s.getsockname()[1]


def _tail_log(n: int = _LOG_TAIL_LINES) -> str:
    if _log_path is None:
        return ""
    try:
        lines = _log_path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def start_managed_qdrant() -> str:
    """Start the managed Qdrant subprocess (idempotent); return its base URL.

    Raises ``RuntimeError`` on any failure — by contract there is no fallback.
    Safe to call repeatedly: a second call while the process is alive returns
    the same URL without spawning again.
    """
    global _proc, _stopping, _url, _api_key, _log_path, _pid_path
    settings = get_settings()

    with _lock:
        if _proc is not None and _proc.poll() is None:
            assert _url is not None
            return _url  # already running

        binary = _resolve_binary(settings.qdrant_binary)
        storage = settings.resolved_qdrant_managed_dir()
        storage.mkdir(parents=True, exist_ok=True)
        _pid_path = storage / "qdrant.pid"
        _log_path = storage / "qdrant.log"

        _sweep_orphan(_pid_path)

        host = settings.qdrant_managed_host
        http_port = _pick_port(host, settings.qdrant_managed_http_port, "HTTP")
        grpc_port = _pick_port(host, settings.qdrant_managed_grpc_port, "gRPC")
        api_key = secrets.token_hex(16)
        url = f"http://{host}:{http_port}"

        # Qdrant reads nested config via QDRANT__SECTION__KEY env vars. Bind
        # localhost only — this process is a private sidecar, never exposed —
        # and require the per-boot API key so nothing else can be mistaken
        # for (or tamper with) our instance.
        env = {
            **os.environ,
            "QDRANT__STORAGE__STORAGE_PATH": str(storage),
            "QDRANT__STORAGE__SNAPSHOTS_PATH": str(storage / "snapshots"),
            "QDRANT__SERVICE__HOST": host,
            "QDRANT__SERVICE__HTTP_PORT": str(http_port),
            "QDRANT__SERVICE__GRPC_PORT": str(grpc_port),
            "QDRANT__SERVICE__API_KEY": api_key,
            "QDRANT__TELEMETRY_DISABLED": "true",
        }
        logger.info(
            "starting managed Qdrant: %s (storage=%s, http=%d, grpc=%d)",
            binary, storage, http_port, grpc_port,
        )
        try:
            with _log_path.open("wb") as log_fh:  # truncate each boot
                # cwd=storage: Qdrant resolves some paths (e.g. ./snapshots/tmp)
                # relative to its CWD even when STORAGE_PATH is set. Launched
                # from a .app bundle the inherited CWD is a read-only filesystem
                # and the child panics at boot — anchor it in the storage dir.
                proc = subprocess.Popen(
                    [binary],
                    env=env,
                    cwd=str(storage),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                )
        except OSError as e:
            raise RuntimeError(
                f"qdrant_mode=managed: failed to launch {binary!r}: {e}"
            ) from e
        with contextlib.suppress(OSError):
            _pid_path.write_text(json.dumps({"pid": proc.pid, "binary": binary}))
        _proc = proc
        _stopping = False
        _url = url
        _api_key = api_key
        atexit.register(stop_managed_qdrant)

    # Wait for readiness outside the lock so a slow start doesn't serialize
    # other callers. On any failure this stops the process and raises.
    _wait_ready(proc, url, api_key, settings.qdrant_managed_startup_timeout_s)

    # Supervise only once ready: before that, failure is _wait_ready's raise.
    threading.Thread(
        target=_watchdog, args=(proc,), daemon=True, name="qdrant-watchdog"
    ).start()
    return url


def _wait_ready(
    proc: subprocess.Popen, url: str, api_key: str, timeout_s: float
) -> None:
    """Ready means OUR child is alive AND answers /readyz with our API key.

    A child death is authoritative and immediate — a 200 from the port means
    nothing if it isn't ours (that's how a foreign Qdrant got adopted once).
    """
    deadline = time.monotonic() + timeout_s
    ready_url = f"{url}/readyz"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            code = proc.returncode
            tail = _tail_log()
            stop_managed_qdrant()
            raise RuntimeError(
                f"qdrant_mode=managed: process exited (code {code}) before "
                "becoming ready — check the binary and storage path."
                + (f"\n--- qdrant.log tail ---\n{tail}" if tail else "")
            )
        try:
            req = urllib.request.Request(ready_url, headers={"api-key": api_key})
            with urllib.request.urlopen(req, timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    logger.info("managed Qdrant ready at %s", url)
                    return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(0.3)
    tail = _tail_log()
    stop_managed_qdrant()
    raise RuntimeError(
        f"qdrant_mode=managed: not ready within {timeout_s:.0f}s "
        f"(last probe error: {last_err})"
        + (f"\n--- qdrant.log tail ---\n{tail}" if tail else "")
    )


def _watchdog(proc: subprocess.Popen) -> None:
    """Hard-fail the app if the sidecar dies mid-run (no respawn, no limping
    along with a dead vector store while files flip to "error" one by one)."""
    proc.wait()
    with _lock:
        # Only the watchdog of the CURRENT child may act: stop_managed_qdrant
        # clears _proc before terminating, and a stop→start cycle swaps in a
        # new proc — either way this watchdog's child is no longer current and
        # its exit is expected, not a failure.
        unexpected = _proc is proc and not _stopping
    if not unexpected:
        return
    logger.critical(
        "managed Qdrant exited unexpectedly (code %s) — hard-failing the app."
        "\n--- qdrant.log tail ---\n%s",
        proc.returncode, _tail_log(),
    )
    _die()


def stop_managed_qdrant() -> None:
    """Terminate the managed subprocess if we started one (idempotent)."""
    global _proc, _stopping, _api_key, _url
    with _lock:
        proc = _proc
        _proc = None
        _stopping = True  # before terminate, so the watchdog sees it on wake
        _api_key = None
        _url = None
        pid_path = _pid_path
    if pid_path is not None:
        with contextlib.suppress(OSError):
            pid_path.unlink()
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
