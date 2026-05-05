"""Google Drive sync connector.

Mirrors a Drive folder (My Drive subfolder, Shared Drive root, or
shared-with-me folder) onto disk under the registered folder root. The
watcher then picks up new/changed/deleted files and the indexing pipeline
takes over — same contract as the GitHub connector.

Native Google types are exported on the way down:

* ``application/vnd.google-apps.spreadsheet``  → ``.xlsx``
* ``application/vnd.google-apps.presentation`` → ``.pptx``
* ``application/vnd.google-apps.document``     → ``.docx`` *unless* the doc
  has tabs (the post-2024 multi-tab feature). In that case we call the Docs
  API with ``includeTabsContent=true`` and write **one markdown file per
  tab** under a same-named directory. Each tab file gets its own row in
  ``.voitta_sources.json`` carrying the deep-link tab URL and the tab
  display name, so chunks indexed from each tab carry a ``tab`` payload all
  the way through to Qdrant.

Other Google-native types (forms, sites, drawings, scripts, fusion tables)
are skipped with a debug log — they have no useful textual export.

Authentication
--------------
Two modes, mirroring voitta-rag:

* **OAuth** — ``gd_client_id`` + ``gd_client_secret`` saved on the source
  row; the user clicks Connect, the unified callback (api/routes/sync.py)
  stores ``gd_refresh_token``. Access tokens are refreshed on demand and
  cached in-process.
* **Service account** — ``gd_service_account_json`` (the entire key file
  as a string). Useful for headless / server-side deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from ._gdoc_markdown import (
    has_tabs,
    render_document_tabs,
    safe_filename,
)

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# We need both readonly Drive (list/download/export) and readonly Docs (tab
# walking). Asking for both up-front avoids a re-consent flow when a user's
# folder has multi-tab Docs.
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/documents.readonly"
)

# Native Google types we know how to export. Anything else under
# ``vnd.google-apps.*`` is silently skipped.
NATIVE_DOC = "application/vnd.google-apps.document"
NATIVE_SHEET = "application/vnd.google-apps.spreadsheet"
NATIVE_SLIDES = "application/vnd.google-apps.presentation"
NATIVE_FOLDER = "application/vnd.google-apps.folder"

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
EXPORT_MAP: dict[str, tuple[str, str]] = {
    NATIVE_SHEET: (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    NATIVE_SLIDES: (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    # NATIVE_DOC is handled specially in `_materialize_doc`.
}

# MIME-level blocklist: applied alongside ``VOITTA_IGNORE_PATTERNS`` so we
# also catch items the filename globs can miss. Meet recordings, voice
# memos, and other audio/video files frequently land in Drive with no
# extension or odd names ("Recording 2026-04-12") — globs like ``*.mp4``
# don't see those, but Drive's ``mimeType`` is reliable. ``image/*`` stays
# un-blocked: this is image-rag, the image pipeline indexes those.
# Archives have stable mimeType in Drive so the explicit set is enough.
_BLOCKED_MIME_PREFIXES = ("audio/", "video/")
_BLOCKED_MIMES = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "application/gzip",
        "application/x-gzip",
        "application/x-bzip2",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/vnd.rar",
        "application/x-iso9660-image",
        "application/x-apple-diskimage",
    }
)


def _is_blocked_mime(mime: str) -> bool:
    """True if Drive's ``mimeType`` flags this as a non-RAG-able blob."""
    return mime.startswith(_BLOCKED_MIME_PREFIXES) or mime in _BLOCKED_MIMES

# Inline header on tab markdown files. Lets the next sync skip a re-render
# without touching the Docs API again.
FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"

_SAFE_NAME_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def _safe_folder_name(name: str, fallback: str = "drive") -> str:
    """Sanitize a Drive folder name for use as a local directory component."""
    out = _SAFE_NAME_RE.sub("-", name).strip().strip(".")
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"-{2,}", "-", out)
    return out[:80] or fallback


def coerce_folders_field(raw: str | None) -> list[dict[str, str]]:
    """Decode the JSON array stored in ``folder_sync_sources.gd_folder_id``.

    Accepts three on-disk shapes for backwards compatibility:

    * ``None`` / empty → ``[]``
    * Legacy plain-string folder ID → wrapped as ``[{"id": <s>, "name": ""}]``
    * JSON array of ``{"id": str, "name": str}`` objects → returned as-is

    Older single-folder rows persisted before multi-folder support landed
    fall through the second branch and keep working.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [{"id": raw, "name": ""}]
    if isinstance(parsed, list):
        out: list[dict[str, str]] = []
        for entry in parsed:
            if isinstance(entry, dict) and "id" in entry:
                out.append(
                    {
                        "id": str(entry["id"]),
                        "name": str(entry.get("name") or ""),
                    }
                )
        return out
    if isinstance(parsed, str):
        return [{"id": parsed, "name": ""}]
    return []


def encode_folders_field(folders: list[dict[str, str]] | None) -> str | None:
    if not folders:
        return None
    cleaned = [
        {"id": str(f["id"]), "name": str(f.get("name") or "")}
        for f in folders
        if isinstance(f, dict) and f.get("id")
    ]
    return json.dumps(cleaned) if cleaned else None


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GoogleDriveAuth:
    """Snapshot of credentials taken before releasing the DB session."""

    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    service_account_json: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.refresh_token or self.service_account_json)


@dataclass
class GoogleDriveSyncStats:
    """Same surface as ``GitSyncStats``: indexing.py is shape-agnostic."""

    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_skipped: int = 0  # matched VOITTA_IGNORE_PATTERNS at enumerate time
    tabs_written: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
            "files_skipped": self.files_skipped,
            "tabs_written": self.tabs_written,
            "errors": self.errors,
        }


@dataclass
class _RemoteEntry:
    """One materialised local file we expect to write for a remote item."""

    rel_path: str
    url: str
    tab: str | None
    # Deferring the actual fetch keeps listing cheap and lets the connector
    # short-circuit on (size, mtime, fingerprint) before touching the network.
    # The second arg is a per-thread ``drive`` Resource — the download phase
    # runs producers from a thread pool and googleapiclient's Resource is
    # NOT thread-safe (it shares ``httplib2.Http`` connection state). Tab
    # producers ignore the arg since they only write text to disk.
    producer: Callable[[Path, Any], None]
    # Stable identity for change detection. For binary downloads this is the
    # Drive ``md5Checksum``; for exports/tabs we use ``modifiedTime`` since
    # exports have no md5.
    fingerprint: str
    size_hint: int | None = None


# ---------------------------------------------------------------------------
# OAuth helpers (callable from API routes — no connector instance needed)
# ---------------------------------------------------------------------------


def get_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Google OAuth2 authorization URL."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        # ``prompt=consent`` forces Google to re-issue a refresh_token even if
        # the user has previously authorized this client. Without it, only
        # the very first authorization yields one.
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(_token_error("exchange", resp))
    return resp.json()


async def list_root_folders(
    *,
    client_id: str = "",
    client_secret: str = "",
    refresh_token: str = "",
    service_account_json: str = "",
) -> dict[str, list[dict[str, str]]]:
    """List Drive locations the user can pick as a sync root.

    Supports both auth modes. With OAuth you get the user's My Drive,
    Shared with me, and Shared Drives. With a service account you get
    Shared with me + Shared Drives only — service accounts have no human
    "My Drive" so that bucket comes back empty (which is correct, not an
    error). Either way the front-end picker renders the same three
    sections.
    """
    if refresh_token:
        access_token = await _refresh_access_token(
            client_id, client_secret, refresh_token
        )
    elif service_account_json:
        # google-auth's refresh() is sync + does network I/O; punt off the
        # event loop so we don't block other requests.
        access_token = await asyncio.to_thread(
            _service_account_access_token, service_account_json
        )
    else:
        raise RuntimeError(
            "Cannot list folders: no OAuth refresh_token and no service-account JSON"
        )

    return await _list_drive_locations(access_token, has_my_drive=bool(refresh_token))


def _service_account_access_token(sa_json: str) -> str:
    """Mint a short-lived OAuth2 access token from a service-account key.

    Uses ``google.oauth2.service_account`` + a ``google.auth.transport``
    request — same library the connector already imports for ``_build_services``,
    just used here to produce a raw bearer token we can hand to httpx.
    """
    import google.auth.transport.requests as ga_req
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=GOOGLE_SCOPES.split()
    )
    creds.refresh(ga_req.Request())
    return creds.token


async def _list_drive_locations(
    access_token: str, *, has_my_drive: bool
) -> dict[str, list[dict[str, str]]]:
    """Run the three Drive enumeration calls behind a bearer token.

    ``has_my_drive`` short-circuits the My Drive query for service-account
    auth — the API would happily return zero files there, but skipping the
    call saves a round-trip and avoids surprising the operator with an
    empty section in the picker.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    base = "https://www.googleapis.com/drive/v3/files"
    # Owner / shared-time / modified-time are returned only when explicitly
    # requested via ``fields``. We ask for all three so the picker can show
    # who the recording belongs to (eight folders all named "Meet Recordings"
    # are nine different people's personal recordings — without owner email
    # the list is unusable).
    files_fields = "files(id,name,owners(displayName,emailAddress),sharedWithMeTime,modifiedTime)"
    common = {"fields": files_fields, "pageSize": "100", "orderBy": "name"}

    async with httpx.AsyncClient() as client:
        my_files_raw: list[dict] = []
        if has_my_drive:
            my = await client.get(
                base,
                headers=headers,
                params={
                    **common,
                    "q": (
                        "'root' in parents and mimeType='application/vnd.google-apps.folder' "
                        "and trashed=false"
                    ),
                },
            )
            if my.status_code != 200:
                raise RuntimeError(f"Drive list (My Drive) failed: {my.text[:300]}")
            my_files_raw = my.json().get("files", [])
        shared = await client.get(
            base,
            headers=headers,
            params={
                **common,
                "q": (
                    "sharedWithMe=true and mimeType='application/vnd.google-apps.folder' "
                    "and trashed=false"
                ),
            },
        )
        if shared.status_code != 200:
            raise RuntimeError(f"Drive list (shared) failed: {shared.text[:300]}")
        drives = await client.get(
            "https://www.googleapis.com/drive/v3/drives",
            headers=headers,
            params={"pageSize": "100"},
        )
        if drives.status_code != 200:
            raise RuntimeError(f"Drive list (drives) failed: {drives.text[:300]}")

    return {
        "folders": [_folder_summary(f) for f in my_files_raw],
        "shared_folders": [
            _folder_summary(f) for f in shared.json().get("files", [])
        ],
        "shared_drives": [
            # Shared Drives don't have owners; surface name + id only.
            {"id": d["id"], "name": d["name"]}
            for d in drives.json().get("drives", [])
        ],
    }


def _folder_summary(f: dict) -> dict[str, str]:
    """Flatten a Drive ``files`` entry to the picker's wire shape.

    Pulls the first owner's email/display_name out of the ``owners`` list
    (Drive returns it as a list; for personal folders there's exactly one
    owner). Empty strings stand in for missing fields so the front-end
    doesn't have to null-check every cell.
    """
    owners = f.get("owners") or []
    owner = owners[0] if owners else {}
    return {
        "id": f["id"],
        "name": f["name"],
        "owner_email": owner.get("emailAddress") or "",
        "owner_name": owner.get("displayName") or "",
        "shared_at": f.get("sharedWithMeTime") or "",
        "modified_at": f.get("modifiedTime") or "",
    }


async def _refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(_token_error("refresh", resp) + ". Try reconnecting Google Drive.")
    return resp.json()["access_token"]


def _token_error(op: str, resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error_description") or body.get("error") or ""
    except Exception:
        err = resp.text[:500]
    return f"Google token {op} failed ({resp.status_code}): {err}"


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class GoogleDriveConnector:
    """Mirror a Google Drive folder/Drive into ``folder_root``."""

    # In-process cache: (client_id, refresh_token) → (access_token, expires_at)
    _token_cache: ClassVar[dict[tuple[str, str], tuple[str, float]]] = {}

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: GoogleDriveAuth,
        drive_folders: list[dict[str, str]],
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> GoogleDriveSyncStats:
        """Mirror Drive content into ``folder_root``.

        ``progress_cb(phase, done, total, detail)`` — optional. Called
        from the worker thread at every phase boundary, per Drive API
        page during listing, and per-file during downloading. Phases:
        ``"connecting"`` → ``"listing"`` → ``"downloading"`` →
        ``"cleaning"`` → ``"done"``. ``detail`` carries phase-specific
        breadcrumbs (``current_folder``, ``folders_done``,
        ``items_seen``) that the SPA renders in the badge so the user
        sees movement during the long ``listing`` phase. The caller
        wraps this into ``folder.sync_progress`` events; see
        ``indexing._run_sync_inner``.
        """
        if not auth.configured:
            raise RuntimeError(
                "Google Drive not connected. Open the folder's sync settings "
                "and click Connect, or supply a service-account JSON."
            )
        if not drive_folders:
            raise RuntimeError(
                "No Drive folders selected. Click Pick to choose at least one."
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        return await asyncio.to_thread(
            self._sync_sync,
            folder_root=folder_root,
            auth=auth,
            progress_cb=progress_cb,
            drive_folders=drive_folders,
        )

    # -- service plumbing ---------------------------------------------------

    def _build_credentials(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> Any:
        """Construct the right ``Credentials`` object for the active auth mode.

        Pulled out so workers in the parallel docs.get pool can mint their
        own ``Resource`` instances without re-importing OAuth/SA libraries
        per thread. ``googleapiclient.Resource`` is NOT thread-safe — the
        underlying ``httplib2.Http`` caches connection state and concurrent
        requests interleave with Drive timeouts and SSL faults. We give
        each worker its own pair of services in ``_build_docs_for_thread``.
        """
        if access_token is not None:
            from google.oauth2.credentials import Credentials

            return Credentials(token=access_token)
        from google.oauth2.service_account import Credentials as SACredentials

        info = json.loads(auth.service_account_json)
        return SACredentials.from_service_account_info(
            info, scopes=GOOGLE_SCOPES.split()
        )

    def _build_services(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> tuple[Any, Any]:
        """Return ``(drive, docs)`` googleapiclient services for the main thread."""
        from googleapiclient.discovery import build

        creds = self._build_credentials(auth, access_token)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        return drive, docs

    def _build_docs_for_thread(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> Any:
        """Per-worker ``docs`` service. Each call builds a fresh
        ``Resource`` (and therefore a fresh ``httplib2.Http``) so the
        thread pool that drives parallel ``documents.get`` calls doesn't
        share connection state — which is exactly what triggers Drive's
        timeout / SSL-error responses when concurrency goes above 1."""
        from googleapiclient.discovery import build

        creds = self._build_credentials(auth, access_token)
        return build("docs", "v1", credentials=creds, cache_discovery=False)

    def _build_drive_for_thread(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> Any:
        """Per-worker ``drive`` service for the parallel download pool.

        Same rationale as ``_build_docs_for_thread``: ``MediaIoBaseDownload``
        eventually drives ``httplib2.Http`` under the hood, so two threads
        sharing one Resource interleave bytes onto the same connection and
        get truncated downloads + SSL faults. Each pool worker calls this
        once on first use and reuses the same Resource for every download
        it runs — one discovery cost per worker, not per download.
        """
        from googleapiclient.discovery import build

        creds = self._build_credentials(auth, access_token)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _sync_access_token(self, auth: GoogleDriveAuth) -> str:
        """Synchronous token refresh for use from ``_sync_sync`` (worker thread)."""
        key = (auth.client_id, auth.refresh_token)
        cached = self._token_cache.get(key)
        if cached and time.time() < cached[1] - 60:
            return cached[0]
        with httpx.Client() as client:
            resp = client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": auth.client_id,
                    "client_secret": auth.client_secret,
                    "refresh_token": auth.refresh_token,
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(_token_error("refresh", resp) + ". Try reconnecting Google Drive.")
        data = resp.json()
        token = data["access_token"]
        ttl = int(data.get("expires_in", 3600))
        self._token_cache[key] = (token, time.time() + ttl)
        return token

    # -- sync orchestration -------------------------------------------------

    def _sync_sync(
        self,
        *,
        folder_root: Path,
        auth: GoogleDriveAuth,
        drive_folders: list[dict[str, str]],
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> GoogleDriveSyncStats:
        # Wrap so call sites can fire-and-forget without a None check.
        # Swallow exceptions inside the callback — a flaky observer must
        # never break the sync itself.
        def _emit(
            phase: str,
            done: int = 0,
            total: int = 0,
            detail: dict[str, Any] | None = None,
        ) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(phase, done, total, detail)
            except Exception:
                logger.exception("sync progress callback raised")

        _emit("connecting")
        access_token = self._sync_access_token(auth) if auth.refresh_token else None
        drive, docs = self._build_services(auth, access_token)

        # Same ignore matcher the watcher uses, so policy is consistent
        # whether files arrive over Drive or via local fs. Matching here
        # also avoids the egress cost of downloading bytes we'd then
        # immediately ignore (a single mp4 can be hundreds of MB).
        from ..ignore import from_settings as _ignore_from_settings

        ignore = _ignore_from_settings()

        stats = GoogleDriveSyncStats()
        entries: list[_RemoteEntry] = []
        # Google Docs need a per-doc ``documents.get`` round-trip to find
        # out whether the doc has tabs. We can't do that during the
        # listing recursion without blocking on each call serially —
        # 300ms x ~250 docs is most of the pain users feel. Defer them
        # here and process via a thread pool below.
        pending_docs: list[tuple[dict[str, Any], str]] = []
        # Each picked Drive folder is mirrored under its own subdirectory so
        # multi-folder syncs can't collide on same-named children. The
        # subdirectory is the folder's display name when we have one (set by
        # the picker UI), with a stable fallback when it's missing — rare
        # legacy single-folder rows where only the ID was stored.
        used_dirs: set[str] = set()
        n_drive_folders = len(drive_folders)

        # Build a per-folder "tick" callback the recursive enumerator can
        # call after every Drive API page. Without this, big folders with
        # thousands of items show a frozen "Listing — 3/11 folders" pill
        # for 30+ seconds; with it, the badge flips through items_seen as
        # pages arrive (every 1000 items, plus once per subfolder
        # boundary). Items_seen is `len(entries)` + the count of items
        # that were skipped via ignore-pattern matching.
        skipped_during_listing = [0]  # boxed counter so closures can mutate

        def _make_listing_tick(
            idx: int, display: str
        ) -> Callable[[], None]:
            def _tick() -> None:
                _emit(
                    "listing",
                    len(entries),
                    0,  # total items unknown until listing completes
                    {
                        "folders_done": idx - 1,
                        "folders_total": n_drive_folders,
                        "current_folder": display or "(unnamed)",
                        "items_seen": len(entries),
                        "items_skipped": skipped_during_listing[0],
                    },
                )

            return _tick

        # Initial event so the SPA flips to "Listing — 0/11" before the
        # first Drive call returns (which can take a few seconds).
        _emit(
            "listing",
            0,
            0,
            {
                "folders_done": 0,
                "folders_total": n_drive_folders,
                "current_folder": "",
                "items_seen": 0,
                "items_skipped": 0,
            },
        )
        for idx, folder in enumerate(drive_folders, start=1):
            folder_id = folder["id"]
            display = folder.get("name") or ""
            base = _safe_folder_name(display) if display else f"drive-{folder_id[:8]}"
            # Disambiguate if two picked folders sanitise to the same dir.
            unique = base
            n = 2
            while unique in used_dirs:
                unique = f"{base}-{n}"
                n += 1
            used_dirs.add(unique)
            tick = _make_listing_tick(idx, display)
            tick()  # entering this folder — flips current_folder in the badge
            try:
                self._enumerate(
                    drive, docs, folder_id, unique, entries, stats, ignore,
                    on_page=tick,
                    skipped_counter=skipped_during_listing,
                    pending_docs=pending_docs,
                )
            except Exception as e:
                logger.exception("Drive enumeration failed for %s", folder_id)
                stats.errors.append(f"list {display or folder_id}: {e}")
            # Emit after each top-level folder so a multi-folder sync flips
            # through "1/3", "2/3", "3/3" — useful when listing a Shared
            # Drive can take a minute.
            _emit(
                "listing",
                len(entries),
                0,
                {
                    "folders_done": idx,
                    "folders_total": n_drive_folders,
                    "current_folder": display or "(unnamed)",
                    "items_seen": len(entries),
                    "items_skipped": skipped_during_listing[0],
                },
            )

        # Phase 2: fan out the docs.get calls accumulated during listing.
        # Each one is independent so they're embarrassingly parallel; we
        # cap concurrency at 4 to stay well under Drive's default per-user
        # rate limits (10 RPS sustained, bursts tolerated). googleapiclient's
        # Resource is NOT thread-safe — ``_thread_local_docs`` gives each
        # worker its own service so concurrent requests don't share an
        # httplib2.Http and trip Drive's timeout / SSL-error responses.
        # Empirically this drops listing time on a 250-doc Meet Recordings
        # tree from ~80s to ~10s with zero timeout fallbacks.
        if pending_docs:
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed

            n_docs = len(pending_docs)
            _emit(
                "fetching_docs",
                0,
                n_docs,
                {"items_seen": len(entries)},
            )

            tl = threading.local()

            def _docs_for_this_thread() -> Any:
                # Build at most one ``docs`` Resource per worker thread,
                # reuse for every task that thread runs. Costs one
                # discovery call per worker (~50ms) instead of one per
                # task (~50ms x N), AND keeps each worker's connection
                # isolated from the others.
                if not hasattr(tl, "docs"):
                    tl.docs = self._build_docs_for_thread(auth, access_token)
                return tl.docs

            def _materialize_one(item: dict, rel_here: str):
                return self._materialize_doc(
                    drive, _docs_for_this_thread(), item, rel_here
                )

            doc_emit_every = max(1, n_docs // 50)
            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="gd-docs"
            ) as pool:
                futures = {
                    pool.submit(_materialize_one, item, rel_here): rel_here
                    for (item, rel_here) in pending_docs
                }
                for completed, fut in enumerate(as_completed(futures), start=1):
                    rel = futures[fut]
                    try:
                        doc_entries, n_tabs = fut.result()
                    except Exception as e:
                        logger.exception("Drive doc fetch failed for %s", rel)
                        stats.errors.append(f"{rel}: {e}")
                    else:
                        entries.extend(doc_entries)
                        stats.tabs_written += n_tabs
                    if completed == n_docs or completed % doc_emit_every == 0:
                        _emit(
                            "fetching_docs",
                            completed,
                            n_docs,
                            {"items_seen": len(entries)},
                        )

        if stats.errors and not entries:
            # Every selected folder failed to enumerate — bail before we
            # delete every local file as "not on remote".
            _emit("done")
            return stats

        expected_paths: set[str] = {e.rel_path for e in entries}
        sidecar: dict[str, dict[str, str]] = {}

        total_entries = len(entries)
        # Throttle per-file progress emits so the WS channel doesn't drown
        # on huge folders — once per file is fine for hundreds, but at 5k+
        # entries the noise adds up.
        emit_every = max(1, total_entries // 100)
        _emit("downloading", 0, total_entries)

        # Phase 3: download in parallel. The Drive API's per-user budget is
        # 200 RPS — we cap at 8 here, which is conservative on RPS but is
        # the sweet spot for typical home-bandwidth syncs (more workers
        # share the same uplink and don't speed each up). Same per-thread
        # ``Resource`` pattern as the docs.get pool: googleapiclient is not
        # thread-safe, so each worker builds its own ``drive`` once and
        # reuses it. Empirically a 200-file sync (mixed exports + binaries
        # + tabs) drops from ~5 min to ~40s on the agnitio-ops box.
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        tl_dl = threading.local()

        def _drive_for_dl_thread() -> Any:
            if not hasattr(tl_dl, "drive"):
                tl_dl.drive = self._build_drive_for_thread(auth, access_token)
            return tl_dl.drive

        def _materialize_one(
            entry: _RemoteEntry,
        ) -> tuple[_RemoteEntry, bool, bool, str | None]:
            """Run the producer for one entry off the main thread.

            Returns ``(entry, was_unchanged, existed_before, error)``. We
            keep stats + sidecar mutation on the main thread (single-
            threaded as ``as_completed`` drains) to avoid a lock.
            """
            local = folder_root / entry.rel_path
            local.parent.mkdir(parents=True, exist_ok=True)
            if self._is_unchanged(local, entry):
                return entry, True, False, None
            existed_before = local.exists()
            try:
                entry.producer(local, _drive_for_dl_thread())
            except Exception as e:
                logger.exception("Drive download failed: %s", entry.rel_path)
                return entry, False, existed_before, str(e)
            return entry, False, existed_before, None

        with ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="gd-dl"
        ) as pool:
            futures = [pool.submit(_materialize_one, e) for e in entries]
            for i, fut in enumerate(as_completed(futures), start=1):
                entry, was_unchanged, existed_before, err = fut.result()
                if err is not None:
                    stats.errors.append(f"{entry.rel_path}: {err}")
                else:
                    self._record_sidecar(sidecar, entry)
                    if not was_unchanged:
                        local = folder_root / entry.rel_path
                        if local.exists() and local.stat().st_size > 0:
                            if existed_before:
                                stats.files_updated += 1
                            else:
                                stats.files_added += 1
                if i == total_entries or i % emit_every == 0:
                    _emit("downloading", i, total_entries)

        _emit("cleaning", 0, 0)
        # Drop locals not on remote (skip our own sidecars).
        keep = {".voitta_sources.json", ".voitta_timestamps.json"}
        for path in list(folder_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(folder_root).as_posix()
            if rel in keep or rel in expected_paths:
                continue
            try:
                path.unlink()
                stats.files_removed += 1
            except OSError as e:
                stats.errors.append(f"unlink {rel}: {e}")

        # Tidy empty directories left behind by deletes.
        for d in sorted(folder_root.rglob("*"), reverse=True):
            if d.is_dir():
                with suppress(OSError):
                    d.rmdir()

        # Write the sidecar last so a partial sync still leaves a coherent
        # mapping for whatever did land on disk.
        (folder_root / ".voitta_sources.json").write_text(
            json.dumps(sidecar, indent=2, sort_keys=True)
        )

        _emit("done", total_entries, total_entries)
        return stats

    # -- enumeration --------------------------------------------------------

    def _enumerate(
        self,
        drive: Any,
        docs: Any,
        folder_id: str,
        rel_prefix: str,
        out: list[_RemoteEntry],
        stats: GoogleDriveSyncStats,
        ignore: Any,  # IgnoreMatcher
        on_page: Callable[[], None] | None = None,
        skipped_counter: list[int] | None = None,
        pending_docs: list[tuple[dict[str, Any], str]] | None = None,
    ) -> None:
        """Recursive Drive listing.

        ``on_page`` fires after every Drive API page is fully processed
        — the caller can use that as a "tick" to update the sync progress
        badge so users see movement during long enumerations rather than
        a frozen "Listing — 3/11" pill. ``skipped_counter[0]`` is bumped
        for every ignore-pattern match so the badge can show
        items_skipped alongside items_seen.
        """
        page_token: str | None = None
        while True:
            resp = (
                drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields=(
                        "nextPageToken,"
                        "files(id,name,mimeType,size,modifiedTime,md5Checksum,webViewLink)"
                    ),
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in resp.get("files", []):
                self._process_item(
                    drive, docs, item, rel_prefix, out, stats, ignore,
                    on_page=on_page,
                    skipped_counter=skipped_counter,
                    pending_docs=pending_docs,
                )
            if on_page is not None:
                on_page()
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def _process_item(
        self,
        drive: Any,
        docs: Any,
        item: dict[str, Any],
        rel_prefix: str,
        out: list[_RemoteEntry],
        stats: GoogleDriveSyncStats,
        ignore: Any,  # IgnoreMatcher
        on_page: Callable[[], None] | None = None,
        skipped_counter: list[int] | None = None,
        pending_docs: list[tuple[dict[str, Any], str]] | None = None,
    ) -> None:
        name = item["name"]
        mime = item["mimeType"]
        rel_here = f"{rel_prefix}/{name}" if rel_prefix else name

        # Apply two skip layers BEFORE we recurse / produce:
        #   1. VOITTA_IGNORE_PATTERNS — filename globs (covers ``*.mp4``,
        #      ``node_modules``, …); the matcher walks ancestors too, so a
        #      file inside an ignored directory is skipped without listing
        #      its contents.
        #   2. MIME blocklist — catches Drive items the filename globs miss
        #      (Meet recordings often have no extension or names like
        #      ``Recording 2026-04-12``; Drive still tags them ``video/mp4``).
        # Native Google docs/sheets/slides stay un-skippable — they're
        # rendered to .md / .xlsx / .pptx which the parsers handle.
        skippable = mime not in (NATIVE_FOLDER, NATIVE_DOC) and mime not in EXPORT_MAP
        if skippable and (ignore.matches(rel_here) or _is_blocked_mime(mime)):
            logger.debug(
                "skipping ignored file: %s (mime=%s)", rel_here, mime
            )
            stats.files_skipped += 1
            if skipped_counter is not None:
                skipped_counter[0] += 1
            return

        if mime == NATIVE_FOLDER:
            # Don't recurse into folders whose name matches an ignore
            # glob (`.git`, `node_modules`, `__pycache__`).
            if ignore.matches(rel_here):
                logger.debug("skipping ignored folder: %s", rel_here)
                return
            self._enumerate(
                drive, docs, item["id"], rel_here, out, stats, ignore,
                on_page=on_page,
                skipped_counter=skipped_counter,
                pending_docs=pending_docs,
            )
            return

        if mime == NATIVE_DOC:
            # Defer the docs.get call. Each Google Doc costs one
            # ``documents.get(includeTabsContent=true)`` round-trip and
            # those are independent — we collect them all here, then run
            # them through a thread pool after listing finishes (see
            # _sync_sync). Used to dominate listing time on Meet
            # Recordings folders (~300ms x N docs serially); now N is
            # capped by the pool's concurrency budget.
            if pending_docs is not None:
                pending_docs.append((item, rel_here))
            else:
                # Defensive fallback for any caller still using the old
                # serial path (tests, future direct callers).
                doc_entries, n_tabs = self._materialize_doc(
                    drive, docs, item, rel_here
                )
                out.extend(doc_entries)
                stats.tabs_written += n_tabs
            return

        if mime in EXPORT_MAP:
            suffix, export_mime = EXPORT_MAP[mime]
            url = item.get("webViewLink") or _drive_view_url(item["id"], mime)
            file_id = item["id"]

            def _producer(
                dest: Path,
                drive: Any,
                _id: str = file_id,
                _m: str = export_mime,
            ) -> None:
                _export_to(drive, _id, _m, dest)

            out.append(
                _RemoteEntry(
                    rel_path=rel_here + suffix,
                    url=url,
                    tab=None,
                    producer=_producer,
                    fingerprint=item.get("modifiedTime", ""),
                )
            )
            return

        if mime.startswith("application/vnd.google-apps."):
            logger.debug("skipping unsupported google-native type: %s (%s)", mime, name)
            return

        # Plain binary file.
        url = item.get("webViewLink") or _drive_view_url(item["id"], mime)
        size = int(item.get("size") or 0)
        file_id = item["id"]

        def _bin_producer(dest: Path, drive: Any, _id: str = file_id) -> None:
            _download_to(drive, _id, dest)

        out.append(
            _RemoteEntry(
                rel_path=rel_here,
                url=url,
                tab=None,
                producer=_bin_producer,
                fingerprint=item.get("md5Checksum") or item.get("modifiedTime", ""),
                size_hint=size,
            )
        )

    def _materialize_doc(
        self,
        drive: Any,
        docs: Any,
        item: dict[str, Any],
        rel_here: str,
    ) -> tuple[list[_RemoteEntry], int]:
        """Decide between a single ``.docx`` and one ``.md`` per tab.

        Returns ``(new_entries, tabs_written)`` instead of mutating shared
        state — this keeps the function safe to call concurrently from a
        thread pool. The Docs API gives us the full structure in one call
        so we render all tabs from that single response (no extra round-
        trips per tab), but we still need one ``documents.get`` per doc
        because Drive doesn't expose a ``hasTabs`` flag in ``files.list``
        — that's exactly the pile of API calls we want to fan out.
        """
        doc_id = item["id"]
        try:
            document = (
                docs.documents()
                .get(documentId=doc_id, includeTabsContent=True)
                .execute()
            )
        except Exception as e:
            # Falling back to a ``.docx`` export is better than skipping the
            # file entirely — the user still gets the primary tab indexed.
            logger.warning(
                "docs.get failed for %s (%s); falling back to docx export",
                rel_here,
                e,
            )
            document = None

        web_url = item.get("webViewLink") or _drive_view_url(doc_id, NATIVE_DOC)
        modified_time = item.get("modifiedTime", "")
        out: list[_RemoteEntry] = []

        if document is not None and has_tabs(document):
            tabs = render_document_tabs(document)
            if tabs:
                base_dir = rel_here  # use the doc title as a directory
                for index, tab in enumerate(tabs, start=1):
                    fname = f"{index:02d}-{safe_filename(tab.title)}.md"
                    rel = f"{base_dir}/{fname}"
                    body = tab.markdown
                    tab_url = f"{web_url.split('?')[0]}?tab={tab.tab_id}"
                    fingerprint = f"{modified_time}#{tab.tab_id}"

                    def _tab_producer(
                        dest: Path,
                        _drive: Any = None,
                        _b: str = body,
                        _fp: str = fingerprint,
                    ) -> None:
                        # Drive arg is unused — tabs are pure local writes,
                        # but the producer signature is uniform so the pool
                        # can call any entry without special-casing.
                        write_tab_with_fingerprint(dest, _fp, _b)

                    out.append(
                        _RemoteEntry(
                            rel_path=rel,
                            url=tab_url,
                            tab=tab.display_name,
                            producer=_tab_producer,
                            fingerprint=fingerprint,
                        )
                    )
                return out, len(tabs)
            logger.info("doc %s reported tabs but none rendered", rel_here)

        # No tabs: export as docx, single file.
        def _docx_producer(dest: Path, drive: Any, _id: str = doc_id) -> None:
            _export_to(drive, _id, DOCX_MIME, dest)

        out.append(
            _RemoteEntry(
                rel_path=rel_here + ".docx",
                url=web_url,
                tab=None,
                producer=_docx_producer,
                fingerprint=modified_time,
            )
        )
        return out, 0

    # -- diffing helpers ----------------------------------------------------

    def _is_unchanged(self, local: Path, entry: _RemoteEntry) -> bool:
        """True when ``local`` already matches ``entry`` and we can skip downloading.

        For markdown tab files we read the inline fingerprint comment we
        wrote on the previous sync — cheap and exact.

        For exports without an md5 (Drive omits md5 on exports), there is
        no perfect short-circuit; treat them as changed so we re-export.
        Drive's modifiedTime alone wouldn't help — we'd still need to write
        the file to compare.

        For plain binaries we compare ``size`` only when an md5 is missing;
        with an md5 we'd need to download to verify, which defeats the
        purpose. Drive is fairly stable about md5, so md5-bearing files
        compared by size + presence is good enough for v1.
        """
        if not local.exists():
            return False
        if entry.rel_path.endswith(".md"):
            try:
                with local.open("r", encoding="utf-8") as f:
                    first = f.readline().strip()
            except OSError:
                return False
            return first == f"{FINGERPRINT_PREFIX}{entry.fingerprint}{FINGERPRINT_SUFFIX}"
        if entry.size_hint is not None and entry.size_hint > 0:
            try:
                return local.stat().st_size == entry.size_hint
            except OSError:
                return False
        return False

    def _record_sidecar(
        self, sidecar: dict[str, dict[str, str]], entry: _RemoteEntry
    ) -> None:
        record: dict[str, str] = {"url": entry.url}
        if entry.tab:
            record["tab"] = entry.tab
        sidecar[entry.rel_path] = record


# ---------------------------------------------------------------------------
# Module-level producers / helpers
# ---------------------------------------------------------------------------


def _atomic_stream_download(downloader_factory: Any, dest: Path) -> None:
    """Stream a Drive download into ``dest`` atomically.

    Writes through a sibling ``.part-<uuid>`` tempfile and ``os.replace``s
    onto the final path on success. Without this, the file appears
    truncated the instant ``dest.open("wb")`` runs and grows as chunks
    arrive — the watcher sees that as a modify event and can queue an
    extract that runs against a half-written xlsx (the "Bad magic
    number for file header" failure we hit during hourly auto-syncs
    once parallel downloads landed and many writes were in-flight at
    once).

    The per-call uuid suffix matters because the same final path can
    legitimately be written by two parallel producers — the user's
    picked Drive folders can overlap (same file reachable through
    multiple parents, or one picked folder nested inside another, or
    a Shared Drive root + a folder inside it both picked). Without a
    unique suffix, both writers race on ``foo.xlsx.part``: the first
    succeeds, the second's ``os.replace`` finds the source missing and
    blows up. With a unique suffix every writer has its own tempfile;
    last-replace-wins on the destination, which is fine — both writers
    fetched the same Drive object so the bytes are identical.

    ``os.replace`` is atomic on POSIX so the watcher sees one event for
    the complete file. On Windows it's atomic when no reader has the
    target open; the watcher does not, so we're safe there too.
    """
    import os
    import uuid

    from googleapiclient.http import MediaIoBaseDownload

    tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("wb") as f:
            request = downloader_factory()
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        os.replace(tmp, dest)
    except Exception:
        # Leave no half-written ``.part`` on disk if the network blew up
        # mid-stream. ``missing_ok=True`` handles the case where the open
        # itself failed before the file was even created.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _export_to(drive: Any, file_id: str, export_mime: str, dest: Path) -> None:
    _atomic_stream_download(
        lambda: drive.files().export_media(fileId=file_id, mimeType=export_mime),
        dest,
    )


def _download_to(drive: Any, file_id: str, dest: Path) -> None:
    _atomic_stream_download(
        lambda: drive.files().get_media(fileId=file_id, supportsAllDrives=True),
        dest,
    )


def write_tab_with_fingerprint(dest: Path, fingerprint: str, body: str) -> None:
    """Write a tab markdown file with the inline fingerprint header.

    ``GoogleDriveConnector._is_unchanged`` reads the first line back to
    skip re-rendering tabs whose Docs ``modifiedTime + tabId`` haven't
    moved since the previous sync.

    Same atomic-write pattern as ``_atomic_stream_download``, including
    the per-call uuid suffix to avoid two parallel writers colliding on
    the same ``.part`` path when the same tab is reachable through
    overlapping picked folders.
    """
    import os
    import uuid

    header = f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n"
    tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(header + body, encoding="utf-8")
        os.replace(tmp, dest)
    except Exception:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _drive_view_url(file_id: str, mime: str) -> str:
    """Fallback web URL when Drive omits ``webViewLink``."""
    if mime == NATIVE_DOC:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    if mime == NATIVE_SHEET:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if mime == NATIVE_SLIDES:
        return f"https://docs.google.com/presentation/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"
