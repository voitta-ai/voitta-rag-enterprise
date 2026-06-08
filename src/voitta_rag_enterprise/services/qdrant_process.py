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
synchronous part of the app lifespan so that raise aborts startup.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from ..config import get_settings

logger = logging.getLogger(__name__)

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


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


def start_managed_qdrant() -> str:
    """Start the managed Qdrant subprocess (idempotent); return its base URL.

    Raises ``RuntimeError`` on any failure — by contract there is no fallback.
    Safe to call repeatedly: a second call while the process is alive returns
    the same URL without spawning again.
    """
    global _proc
    settings = get_settings()
    url = settings.managed_qdrant_url()

    with _lock:
        if _proc is not None and _proc.poll() is None:
            return url  # already running

        binary = _resolve_binary(settings.qdrant_binary)
        storage = settings.resolved_qdrant_managed_dir()
        storage.mkdir(parents=True, exist_ok=True)

        # Qdrant reads nested config via QDRANT__SECTION__KEY env vars. Bind
        # localhost only — this process is a private sidecar, never exposed.
        env = {
            **os.environ,
            "QDRANT__STORAGE__STORAGE_PATH": str(storage),
            "QDRANT__SERVICE__HOST": settings.qdrant_managed_host,
            "QDRANT__SERVICE__HTTP_PORT": str(settings.qdrant_managed_http_port),
            "QDRANT__SERVICE__GRPC_PORT": str(settings.qdrant_managed_grpc_port),
            "QDRANT__TELEMETRY_DISABLED": "true",
        }
        logger.info(
            "starting managed Qdrant: %s (storage=%s, http=%d, grpc=%d)",
            binary, storage, settings.qdrant_managed_http_port,
            settings.qdrant_managed_grpc_port,
        )
        try:
            proc = subprocess.Popen(
                [binary],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            raise RuntimeError(
                f"qdrant_mode=managed: failed to launch {binary!r}: {e}"
            ) from e
        _proc = proc
        atexit.register(stop_managed_qdrant)

    # Wait for readiness outside the lock so a slow start doesn't serialize
    # other callers. On any failure this stops the process and raises.
    _wait_ready(proc, url, settings.qdrant_managed_startup_timeout_s)
    return url


def _wait_ready(proc: subprocess.Popen, url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    ready_url = f"{url}/readyz"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stop_managed_qdrant()
            raise RuntimeError(
                f"qdrant_mode=managed: process exited (code {proc.returncode}) "
                "before becoming ready — check the binary and storage path."
            )
        try:
            with urllib.request.urlopen(ready_url, timeout=2) as r:
                if r.status == 200:
                    logger.info("managed Qdrant ready at %s", url)
                    return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(0.3)
    stop_managed_qdrant()
    raise RuntimeError(
        f"qdrant_mode=managed: not ready within {timeout_s:.0f}s "
        f"(last probe error: {last_err})"
    )


def stop_managed_qdrant() -> None:
    """Terminate the managed subprocess if we started one (idempotent)."""
    global _proc
    with _lock:
        proc = _proc
        _proc = None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
