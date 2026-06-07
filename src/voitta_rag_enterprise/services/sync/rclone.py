"""rclone sync connector.

Mirrors a cloud remote (Google Drive, OneDrive / SharePoint, and — in
principle — any of rclone's ~70 backends) into ``folder_root`` by shelling
out to the ``rclone`` binary. The watcher then picks up the mirrored files
and the indexing pipeline takes over — same contract as every other
connector; this module never touches Qdrant or SQLite directly.

Why this exists alongside the native Drive / SharePoint connectors
------------------------------------------------------------------
The native connectors talk to each provider's REST API directly, which
means OAuth needs a client the *admin* registered in GCP / Azure. rclone
ships its **own built-in public OAuth client**, so a user can connect a
Drive or SharePoint folder with no admin-side app registration at all.
The trade-off: rclone copies native Google Docs as *exported* Office files
rather than the markdown+sidecar export the native Drive connector produces.
For most folders that's fine — the extractor pipeline reads .docx/.xlsx/.pptx.

Auth
----
Both auth shapes collapse to a single stored ``rc_token`` blob plus an
optional ``rc_config_extra`` JSON object of backend params:

* **UI Connect** — server-side browser OAuth using rclone's built-in client
  (``api/routes/sync.py`` reuses the unified OAuth callback). The callback
  formats Google's / Microsoft's token response into rclone's token JSON.
* **Paste** — the user runs ``rclone authorize "<backend>"`` on their own
  machine and pastes the resulting token JSON, or pastes a whole rclone
  remote-config block (``[name]`` + ``type =`` + ``token =`` + params);
  :func:`parse_pasted_config` splits that into ``rc_token`` +
  ``rc_config_extra``.

Sync model
----------
``rclone sync`` — a true **mirror**: files no longer on the remote are
deleted locally, matching the mirror-delete behaviour of the native Drive
and SharePoint connectors. Native Google Docs are exported to Office formats
(``--drive-export-formats``) so the extractor pipeline can read them.

The ``rclone`` config never lands on disk in a world-readable spot: every
invocation writes a private temp dir (mode 700) with a ``rclone.conf``
(mode 600) holding the token, and removes it when the process exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import SyncConnector

logger = logging.getLogger(__name__)

# rclone backend types we expose in the UI. The value is rclone's ``type =``.
SUPPORTED_BACKENDS = ("drive", "onedrive")

# Office export formats requested from Google Workspace native types so the
# extractor pipeline (which reads .docx/.xlsx/.pptx) can index them. ``svg``
# covers Drawings; rclone falls back gracefully when a type has no match.
DRIVE_EXPORT_FORMATS = "docx,xlsx,pptx,svg"

# The single remote name we write into every temp config. Arbitrary — the
# config only ever holds one remote per invocation.
REMOTE = "voitta"


def rclone_bin() -> str:
    """Path to the rclone binary. ``VOITTA_RCLONE_BIN`` overrides the PATH lookup."""
    return os.environ.get("VOITTA_RCLONE_BIN") or "rclone"


def rclone_available() -> bool:
    """True when the rclone binary is resolvable — gates the connector in the UI."""
    bin_ = rclone_bin()
    if os.path.sep in bin_:
        return os.path.isfile(bin_) and os.access(bin_, os.X_OK)
    return shutil.which(bin_) is not None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class RcloneSyncStats:
    """Same surface as the other connectors so ``indexing.py`` stays shape-agnostic."""

    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
            "files_skipped": self.files_skipped,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Config (de)serialisation
# ---------------------------------------------------------------------------


def parse_pasted_config(text: str) -> tuple[str, str, dict[str, str]]:
    """Split a pasted rclone secret into ``(backend, token_json, extra_params)``.

    Accepts two shapes the user might paste:

    * **A whole remote-config block** — e.g. ``rclone config show`` output::

          [gd]
          type = drive
          token = {"access_token": ...}
          team_drive = 0AB...

      We read ``type`` as the backend, ``token`` as the token JSON, and every
      other ``key = value`` line as an extra param. The ``[name]`` header is
      ignored (we always re-emit under :data:`REMOTE`).

    * **A bare token JSON** — just the ``{"access_token": ...}`` object printed
      by ``rclone authorize``. Backend comes back empty (the caller supplies it
      from the UI's backend picker) and there are no extra params.

    Returns ``("", "", {})``-ish partial tuples for whatever it can recover;
    the caller validates completeness.
    """
    text = (text or "").strip()
    if not text:
        return "", "", {}

    # Bare token JSON?
    if text.startswith("{"):
        # Validate it parses, but store the original text (rclone wants the
        # exact JSON string in the config, and round-tripping could reorder).
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return "", "", {}
        return "", text, {}

    backend = ""
    token = ""
    extra: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "type":
            backend = value
        elif key == "token":
            token = value
        elif value and key in (
            "client_id", "client_secret", "drive_id", "drive_type",
            "team_drive", "root_folder_id", "scope", "region",
        ):
            # Allowlist the params we know are safe + useful to carry. Anything
            # else (e.g. a stray ``[remote]`` artifact) is dropped rather than
            # injected verbatim into the config we generate.
            extra[key] = value
    return backend, token, extra


def encode_extra(extra: dict[str, str] | None) -> str | None:
    if not extra:
        return None
    return json.dumps(extra, sort_keys=True)


def coerce_extra(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


# ---------------------------------------------------------------------------
# Auth snapshot
# ---------------------------------------------------------------------------


@dataclass
class RcloneAuth:
    """Credentials snapshot taken before releasing the DB session."""

    backend: str = ""
    token: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    # Set by the connector after a sync when rclone refreshed (and thus
    # rotated) the token in its temp config. OneDrive/Microsoft rotate the
    # refresh token on every refresh and invalidate the old one, so this MUST
    # be persisted back to ``rc_token`` or the next auto-sync fails. Google
    # doesn't rotate, but persisting the fresher access token is harmless.
    # ``indexing.run_sync`` reads this off the auth object post-sync — same
    # pattern as the Microsoft connector's ``rotated_refresh_token``.
    rotated_token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.backend and self.token)


# ---------------------------------------------------------------------------
# Temp-config plumbing
# ---------------------------------------------------------------------------


def _write_temp_config(auth: RcloneAuth, *, dirpath: Path) -> Path:
    """Write a private ``rclone.conf`` holding the one remote; return its path.

    ``dirpath`` is created mode 700 by the caller; the config file is written
    mode 600. The token (a refresh-token-bearing secret) therefore never sits
    in a world-readable location.
    """
    lines = [f"[{REMOTE}]", f"type = {auth.backend}"]
    for key, value in sorted(auth.extra.items()):
        lines.append(f"{key} = {value}")
    # Token last so a malformed extra line can't swallow it.
    lines.append(f"token = {auth.token}")
    conf = dirpath / "rclone.conf"
    conf.write_text("\n".join(lines) + "\n")
    conf.chmod(0o600)
    return conf


def _read_config_token(conf: Path) -> str:
    """Extract the (possibly rclone-refreshed) ``token = …`` value from a config.

    Returns the raw JSON string, or ``""`` if absent/unreadable. Used to detect
    a rotated token after a sync so it can be persisted back.
    """
    try:
        text = conf.read_text()
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("token") and "=" in line:
            return line.partition("=")[2].strip()
    return ""


def _write_excludes(dirpath: Path) -> Path | None:
    """Translate VOITTA_IGNORE_PATTERNS into an rclone ``--exclude-from`` file.

    Each name-glob ``P`` becomes two rclone filter lines: ``P`` (exclude a
    file/dir named ``P`` at any depth) and ``P/**`` (exclude the contents of a
    dir named ``P``). This mirrors :class:`IgnoreMatcher`, which matches any
    path component, so policy stays consistent whether files arrive over
    rclone or are scanned off local fs.
    """
    from ..ignore import from_settings as _ignore_from_settings

    globs = list(_ignore_from_settings()._patterns)
    if not globs:
        return None
    lines: list[str] = []
    for pat in globs:
        lines.append(pat)
        lines.append(f"{pat}/**")
    path = dirpath / "excludes.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Folder picker (called from the API route)
# ---------------------------------------------------------------------------


def list_remote_dirs(auth: RcloneAuth, parent: str = "") -> list[dict[str, str]]:
    """Return immediate subdirectories of ``parent`` within the remote.

    Powers the sync modal's folder picker. ``parent`` is a path relative to
    the remote root (empty = root). Returns ``[{"name", "path"}]`` sorted by
    name. Raises ``RuntimeError`` with rclone's stderr on failure so the route
    can surface an actionable message (bad token → reconnect).
    """
    if not auth.configured:
        raise RuntimeError("rclone remote not configured (missing backend or token).")
    with tempfile.TemporaryDirectory(prefix="voitta-rclone-") as td:
        d = Path(td)
        d.chmod(0o700)
        conf = _write_temp_config(auth, dirpath=d)
        target = f"{REMOTE}:{parent}" if parent else f"{REMOTE}:"
        proc = subprocess.run(
            [
                rclone_bin(), "lsjson", target,
                "--dirs-only", "--config", str(conf),
                "--no-modtime", "--low-level-retries", "2",
            ],
            capture_output=True, text=True, timeout=120,
        )
    if proc.returncode != 0:
        raise RuntimeError(_rclone_error("list folders", proc.stderr))
    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"rclone lsjson returned non-JSON: {e}") from None
    out: list[dict[str, str]] = []
    for it in items:
        if not it.get("IsDir"):
            continue
        name = it.get("Name") or ""
        rel = f"{parent.rstrip('/')}/{name}" if parent else name
        out.append({"name": name, "path": rel})
    out.sort(key=lambda r: r["name"].lower())
    return out


def _rclone_error(op: str, stderr: str) -> str:
    """Condense rclone's (often multi-line) stderr into one actionable string."""
    tail = (stderr or "").strip().splitlines()
    msg = tail[-1] if tail else "(no output)"
    lower = (stderr or "").lower()
    if "token" in lower and ("expired" in lower or "invalid" in lower or "refresh" in lower):
        msg += " — try reconnecting (the rclone token may have expired)."
    return f"rclone {op} failed: {msg}"


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class RcloneConnector(SyncConnector):
    """Mirror a cloud remote into ``folder_root`` via the rclone binary."""

    source_type = "rclone"
    supports_progress = True

    def resolve_config(self, row) -> dict:
        return {
            "auth": RcloneAuth(
                backend=row.rc_backend or "",
                token=row.rc_token or "",
                extra=coerce_extra(row.rc_config_extra),
            ),
            "root": row.rc_root or "",
            "export_native": bool(row.rc_export_native),
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: RcloneAuth,
        root: str = "",
        export_native: bool = True,
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> RcloneSyncStats:
        if not rclone_available():
            raise RuntimeError(
                "rclone binary not found. Install rclone on the server "
                "(or set VOITTA_RCLONE_BIN) to use this connector."
            )
        if not auth.configured:
            raise RuntimeError(
                "rclone remote not connected. Open the folder's sync settings "
                "and click Connect, or paste an rclone token."
            )
        if auth.backend not in SUPPORTED_BACKENDS:
            raise RuntimeError(
                f"Unsupported rclone backend {auth.backend!r} "
                f"(expected one of {', '.join(SUPPORTED_BACKENDS)})."
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        return await asyncio.to_thread(
            self._sync_sync,
            folder_root=folder_root,
            auth=auth,
            root=root,
            export_native=export_native,
            progress_cb=progress_cb,
        )

    # -- worker -------------------------------------------------------------

    def _sync_sync(
        self,
        *,
        folder_root: Path,
        auth: RcloneAuth,
        root: str,
        export_native: bool,
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None] | None,
    ) -> RcloneSyncStats:
        def _emit(
            phase: str, done: int = 0, total: int = 0,
            detail: dict[str, Any] | None = None,
        ) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(phase, done, total, detail)
            except Exception:
                logger.exception("rclone sync progress callback raised")

        stats = RcloneSyncStats()
        _emit("connecting")

        with tempfile.TemporaryDirectory(prefix="voitta-rclone-") as td:
            d = Path(td)
            d.chmod(0o700)
            conf = _write_temp_config(auth, dirpath=d)
            excludes = _write_excludes(d)

            src = f"{REMOTE}:{root}" if root else f"{REMOTE}:"
            cmd = [
                rclone_bin(), "sync", src, str(folder_root),
                "--config", str(conf),
                "--use-json-log", "-v",
                "--stats", "1s", "--stats-log-level", "NOTICE",
                "--transfers", "8", "--checkers", "16",
                "--fast-list", "--low-level-retries", "3",
                # Trust the remote's own size/hash for change detection rather
                # than re-downloading; matches the native connectors' diffing.
                "--track-renames",
            ]
            if excludes is not None:
                cmd += ["--exclude-from", str(excludes)]
            if auth.backend == "drive" and export_native:
                cmd += ["--drive-export-formats", DRIVE_EXPORT_FORMATS]

            self._run_streaming(cmd, stats=stats, emit=_emit)

            # rclone rewrites ``token = …`` in the config in place when it
            # refreshes. Read it back (still inside the temp-dir context) and
            # park any change on the auth object so run_sync can persist it —
            # critical for OneDrive, whose refresh tokens rotate per refresh.
            new_token = _read_config_token(conf)
            if new_token and new_token != auth.token:
                auth.rotated_token = new_token

        _emit("done", stats.files_added + stats.files_updated,
              stats.files_added + stats.files_updated)
        return stats

    def _run_streaming(
        self,
        cmd: list[str],
        *,
        stats: RcloneSyncStats,
        emit: Callable[..., None],
    ) -> None:
        """Run rclone, parsing its JSON log stream for progress + counts.

        ``--use-json-log`` makes every log line a JSON object on stderr. Two
        kinds matter:

        * **per-object** lines (level ``info`` under ``-v``) — ``"Copied
          (new)"`` / ``"Copied (replaced existing)"`` / ``"Deleted"`` /
          ``"Unchanged skipping"`` give us exact add/update/remove/skip counts.
        * **stats** lines (every ``--stats`` tick) carry a ``stats`` object
          with ``transfers`` / ``totalTransfers`` we surface as the
          ``downloading`` progress bar.
        """
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        assert proc.stderr is not None
        last_total = 0
        try:
            for line in proc.stderr:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON noise (shouldn't happen with --use-json-log, but
                    # rclone occasionally prints a bare banner). Ignore.
                    continue
                st = rec.get("stats")
                if isinstance(st, dict):
                    done = int(st.get("transfers") or 0)
                    total = int(st.get("totalTransfers") or 0) or last_total
                    last_total = total
                    emit("downloading", done, total,
                         {"bytes": st.get("bytes"), "total_bytes": st.get("totalBytes")})
                    continue
                msg = rec.get("msg") or ""
                obj = rec.get("object")
                if not obj or "file system at" in str(obj).lower():
                    # Backend banner lines carry the remote as ``object`` —
                    # skip them; only real file paths count.
                    continue
                if rec.get("level") == "error":
                    stats.errors.append(f"{obj}: {msg}")
                elif msg.startswith("Copied"):
                    # Cloud→local downloads say "Copied (new)" /
                    # "Copied (replaced existing)"; same-backend ops say
                    # "Copied (server-side copy)" (can't tell new vs replace —
                    # count as added). ``Updated`` covers metadata-only writes.
                    if "replaced existing" in msg:
                        stats.files_updated += 1
                    else:
                        stats.files_added += 1
                elif msg.startswith(("Updated", "Renamed", "Moved")):
                    stats.files_updated += 1
                elif msg.startswith("Deleted"):
                    stats.files_removed += 1
                elif msg.startswith("Unchanged"):
                    stats.files_skipped += 1
        finally:
            proc.wait()
        # rclone exits non-zero on partial failure; if we already captured
        # per-object errors, surface those, else the generic code.
        if proc.returncode != 0 and not stats.errors:
            stats.errors.append(f"rclone exited with code {proc.returncode}")
