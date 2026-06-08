"""First-launch installer for the heavy stack + managed Qdrant binary.

Three steps, each idempotent and resumable via ``<data>/install_state.json``:

  1. **deps**   — pip-install the ML/server stack from the bundled, fully
     *pinned* ``requirements-lock.txt``. Pinned input means pip does no
     resolution, which is exactly what avoids the "resolution-too-deep"
     blow-up a bare ``pip install .`` hits on this dependency graph. A
     one-shot ``-r`` is tried first; on failure we retry line-by-line so a
     single bad wheel doesn't sink the whole run.
  2. **qdrant** — download the native Qdrant binary (Apple-Silicon) from the
     official GitHub release into ``userbase/bin/qdrant`` (managed mode).
  3. **models** — best-effort prewarm of the HF model weights so the first
     real query isn't slow. Never fails the install.

The install runs into ``PIP_PREFIX`` (``userbase/``), set by ``__main__``.
Progress is reported through an :class:`InstallReporter` — the base prints to
stdout (the logfile); the desktop shell subclasses it to drive the phase rows
and log view of the first-run :class:`~.install_window.InstallWindow`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ._version import __version__


class InstallReporter:
    """Sink for install progress. The base implementation prints everything to
    stdout (which the desktop shell points at the logfile); the desktop shell
    subclasses it to also drive the ``InstallWindow`` rows. Phase indices match
    ``_STEPS``.
    """

    def phase_start(self, phase: int, label: str = "Running…") -> None:
        print(f"[setup] ▶ {label}", flush=True)

    def phase_progress(self, phase: int, current: int, total: int, label: str) -> None:
        pass

    def phase_done(self, phase: int, note: str = "Done") -> None:
        print(f"[setup] ✓ {note}", flush=True)

    def phase_skip(self, phase: int, note: str = "Already installed") -> None:
        print(f"[setup] ↷ {note}", flush=True)

    def phase_fail(self, phase: int, reason: str) -> None:
        print(f"[setup] ✗ {reason}", flush=True)

    def log(self, line: str) -> None:
        print(line, flush=True)


# Back-compat alias for the old 4-arg callback type (no longer used internally).
ProgressCb = Callable[[int, int, str, "str | None"], None]

# Pinned Qdrant release for the managed binary (matches what we tested).
_QDRANT_VERSION = "1.18.2"
_QDRANT_ASSET = "qdrant-aarch64-apple-darwin.tar.gz"
_QDRANT_URL = (
    f"https://github.com/qdrant/qdrant/releases/download/"
    f"v{_QDRANT_VERSION}/{_QDRANT_ASSET}"
)

_STEPS = ("deps", "qdrant", "models")

# Bump to force every client to re-run the install regardless of inputs.
_STEPS_VERSION = "1"


def _ensure_ca_env() -> ssl.SSLContext:
    """Wire a working CA bundle into TLS for the bundled (framework) Python.

    A frozen macOS .app runs Briefcase's Python.framework, whose ``urllib``
    has **no CA store** by default → ``CERTIFICATE_VERIFY_FAILED`` on any HTTPS
    fetch (the Qdrant binary download, HF model downloads, …). ``certifi`` is
    pip-installed in the ``deps`` step, so by the time we hit any network step
    it's importable. Export ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` so every
    library in this process (and child processes / the server thread that runs
    later) verifies correctly, and return an explicit context for urllib calls.
    """
    try:
        import certifi

        ca = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
        return ssl.create_default_context(cafile=ca)
    except Exception:
        # certifi missing (shouldn't happen post-deps) — fall back to the
        # system default context rather than disabling verification.
        return ssl.create_default_context()


def _state_path() -> Path:
    return Path(os.environ["VOITTA_DATA_DIR"]).parent / "install_state.json"


def _load_state() -> dict:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _fingerprint(resources_dir: Path) -> str:
    """Identity of what the install *produces*, independent of the app version.

    Keyed on the actual inputs — the pinned dependency set and the Qdrant
    version — so bumping the app version on every build does NOT trigger a full
    reinstall, while a genuine change to the lock file or Qdrant version does.
    Only the dependency *specs* are hashed (not comments/ordering) so a
    regenerated lock with an unchanged pin set keeps the same fingerprint.
    """
    h = hashlib.sha256()
    h.update(_STEPS_VERSION.encode())
    h.update(_QDRANT_VERSION.encode())
    lock = _lock_file(resources_dir)
    if lock is not None:
        try:
            specs = sorted(
                ln.split("#", 1)[0].strip()
                for ln in lock.read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            )
            h.update("\n".join(specs).encode())
        except OSError:
            pass
    return h.hexdigest()


def _save_state(done_steps: set[str], fingerprint: str) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "app_version": __version__,  # informational only
                "done": sorted(done_steps),
                "ts": time.time(),
            },
            indent=2,
        )
    )


def _completed_steps(resources_dir: Path) -> set[str]:
    """Steps already done for the *current inputs* (see ``_fingerprint``)."""
    state = _load_state()
    if state.get("fingerprint") != _fingerprint(resources_dir):
        return set()
    done = state.get("done")
    return set(done) if isinstance(done, list) else set()


def is_install_complete(resources_dir: Path) -> bool:
    return _completed_steps(resources_dir) >= set(_STEPS)


# ---------------------------------------------------------------------------
# Step 1 — pip deps from the pinned lock
# ---------------------------------------------------------------------------


def _lock_file(resources_dir: Path) -> Path | None:
    p = resources_dir / "requirements-lock.txt"
    return p if p.is_file() else None


def _pip(args: list[str]) -> int:
    """Run pip in-process via its internal API (briefcase has no python -m pip)."""
    from pip._internal.cli.main import main as pip_main

    return pip_main(args)


def _install_deps(resources_dir: Path, reporter: InstallReporter, phase: int) -> None:
    lock = _lock_file(resources_dir)
    if lock is None:
        raise RuntimeError(
            "requirements-lock.txt missing from the bundle — build is broken."
        )
    base = ["install", "--no-warn-script-location", "--disable-pip-version-check"]
    # Bulk install first — fast, and pip's own output (Collecting…/Installing…)
    # streams into the log via the shell's stdout tee, so the bar stays
    # indeterminate (set by phase_start) while lines scroll past.
    reporter.log(f">>> pip install -r {lock.name}")
    rc = _pip([*base, "-r", str(lock)])
    if rc == 0:
        return
    # Fallback: install line-by-line so one bad pin doesn't sink everything —
    # and the per-package count drives a determinate progress bar.
    reporter.log(f"!!! bulk install rc={rc}; retrying per-package")
    specs = [
        ln.split("#", 1)[0].strip()
        for ln in lock.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    total = len(specs)
    failures: list[str] = []
    for i, spec in enumerate(specs):
        reporter.phase_progress(phase, i, total, f"Installing {spec}  ({i + 1}/{total})")
        if _pip([*base, spec]) != 0:
            failures.append(spec)
    if failures:
        raise RuntimeError(
            "pip failed for: " + ", ".join(failures[:5])
            + (" …" if len(failures) > 5 else "")
        )


# ---------------------------------------------------------------------------
# Step 2 — managed Qdrant binary
# ---------------------------------------------------------------------------


def _qdrant_dest() -> Path:
    return Path(os.environ["VOITTA_QDRANT_BINARY"])


def _install_qdrant(reporter: InstallReporter, phase: int) -> None:
    dest = _qdrant_dest()
    if dest.is_file() and os.access(dest, os.X_OK):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    ctx = _ensure_ca_env()
    reporter.log(f">>> downloading Qdrant {_QDRANT_VERSION}")
    reporter.log(f">>> {_QDRANT_URL}")
    with tempfile.TemporaryDirectory(prefix="voitta-qdrant-") as td:
        tgz = Path(td) / _QDRANT_ASSET
        # Explicit certifi-backed context — the bundled Python's urllib has no
        # default CA store. Stream the body so we can drive a byte-progress bar.
        with urllib.request.urlopen(_QDRANT_URL, context=ctx) as r, tgz.open("wb") as f:  # noqa: S310 — pinned official URL
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            mb = 1024 * 1024
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    reporter.phase_progress(
                        phase, got, total,
                        f"Downloading Qdrant… {got // mb} / {total // mb} MB",
                    )
        with tarfile.open(tgz) as tar:
            member = next(
                (m for m in tar.getmembers() if Path(m.name).name == "qdrant"), None
            )
            if member is None:
                raise RuntimeError("qdrant binary not found in the release tarball")
            member.name = "qdrant"
            tar.extract(member, path=dest.parent)
    dest.chmod(0o755)


# ---------------------------------------------------------------------------
# Step 3 — model prewarm (best-effort)
# ---------------------------------------------------------------------------


def _prewarm_models(reporter: InstallReporter, phase: int) -> None:
    # HuggingFace downloads over HTTPS — same CA story as the Qdrant fetch.
    # Setting the env here also persists into the server thread (same process)
    # so its own first-query model pulls verify correctly.
    _ensure_ca_env()
    reporter.log(">>> warming embedders (e5 · SigLIP)")
    try:
        # Importing + touching the factory triggers the lazy HF downloads the
        # server would otherwise do on the first query.
        from voitta_rag_enterprise.services.embedding import factory  # noqa: PLC0415

        emb = factory.get_text_embedder()
        emb.embed_query("warmup")
    except Exception as exc:  # noqa: BLE001 — never fail install on prewarm
        # Best-effort: models download lazily on first use if this is skipped.
        reporter.log(f"!!! model prewarm skipped: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


_STEP_LABELS = {
    "deps": "Python packages",
    "qdrant": "Search engine",
    "models": "AI models",
}


def install_all(resources_dir: Path, reporter: InstallReporter) -> None:
    """Run all not-yet-done steps, driving ``reporter``. Raises on a hard
    failure in deps/qdrant; model prewarm is best-effort and never raises."""
    fingerprint = _fingerprint(resources_dir)
    done = _completed_steps(resources_dir)
    runners = {
        "deps": _install_deps,
        "qdrant": _install_qdrant,
        "models": _prewarm_models,
    }
    for idx, step in enumerate(_STEPS):
        label = _STEP_LABELS[step]
        if step in done:
            reporter.phase_skip(idx, "Already installed")
            continue
        reporter.phase_start(idx, f"{label}…")
        try:
            if step == "deps":
                runners[step](resources_dir, reporter, idx)
            else:
                runners[step](reporter, idx)
        except Exception as exc:  # noqa: BLE001 — surface, mark the row, re-raise
            reporter.phase_fail(idx, str(exc))
            raise
        done.add(step)
        _save_state(done, fingerprint)
        reporter.phase_done(idx)
    reporter.log("Setup complete — starting server…")
