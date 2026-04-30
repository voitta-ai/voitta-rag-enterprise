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

# Inline header on tab markdown files. Lets the next sync skip a re-render
# without touching the Docs API again.
FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"


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
    tabs_written: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
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
    producer: Callable[[Path], None]
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
    client_id: str, client_secret: str, refresh_token: str
) -> dict[str, list[dict[str, str]]]:
    """List Drive locations the user can pick as a sync root."""
    access_token = await _refresh_access_token(client_id, client_secret, refresh_token)
    headers = {"Authorization": f"Bearer {access_token}"}
    base = "https://www.googleapis.com/drive/v3/files"
    common = {"fields": "files(id,name)", "pageSize": "100", "orderBy": "name"}

    async with httpx.AsyncClient() as client:
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

    def _ids(payload: dict, key: str) -> list[dict[str, str]]:
        return [{"id": f["id"], "name": f["name"]} for f in payload.get(key, [])]

    return {
        "folders": _ids(my.json(), "files"),
        "shared_folders": _ids(shared.json(), "files"),
        "shared_drives": _ids(drives.json(), "drives"),
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
        drive_folder_id: str,
    ) -> GoogleDriveSyncStats:
        if not auth.configured:
            raise RuntimeError(
                "Google Drive not connected. Open the folder's sync settings "
                "and click Connect, or supply a service-account JSON."
            )
        if not drive_folder_id:
            raise RuntimeError("drive_folder_id is required")

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        return await asyncio.to_thread(
            self._sync_sync,
            folder_root=folder_root,
            auth=auth,
            drive_folder_id=drive_folder_id,
        )

    # -- service plumbing ---------------------------------------------------

    def _build_services(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> tuple[Any, Any]:
        """Return ``(drive, docs)`` googleapiclient services."""
        from googleapiclient.discovery import build

        if access_token is not None:
            from google.oauth2.credentials import Credentials

            creds = Credentials(token=access_token)
        else:
            from google.oauth2.service_account import Credentials as SACredentials

            info = json.loads(auth.service_account_json)
            creds = SACredentials.from_service_account_info(
                info, scopes=GOOGLE_SCOPES.split()
            )
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        return drive, docs

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
        drive_folder_id: str,
    ) -> GoogleDriveSyncStats:
        access_token = self._sync_access_token(auth) if auth.refresh_token else None
        drive, docs = self._build_services(auth, access_token)

        stats = GoogleDriveSyncStats()
        entries: list[_RemoteEntry] = []
        try:
            self._enumerate(drive, docs, drive_folder_id, "", entries, stats)
        except Exception as e:
            logger.exception("Drive enumeration failed")
            stats.errors.append(f"list: {e}")
            return stats

        expected_paths: set[str] = {e.rel_path for e in entries}
        sidecar: dict[str, dict[str, str]] = {}

        for entry in entries:
            local = folder_root / entry.rel_path
            local.parent.mkdir(parents=True, exist_ok=True)
            if self._is_unchanged(local, entry):
                self._record_sidecar(sidecar, entry)
                continue
            existed_before = local.exists()
            try:
                entry.producer(local)
            except Exception as e:
                logger.exception("Drive download failed: %s", entry.rel_path)
                stats.errors.append(f"{entry.rel_path}: {e}")
                continue
            if local.exists() and local.stat().st_size > 0:
                if existed_before:
                    stats.files_updated += 1
                else:
                    stats.files_added += 1
            self._record_sidecar(sidecar, entry)

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
    ) -> None:
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
                self._process_item(drive, docs, item, rel_prefix, out, stats)
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
    ) -> None:
        name = item["name"]
        mime = item["mimeType"]
        rel_here = f"{rel_prefix}/{name}" if rel_prefix else name

        if mime == NATIVE_FOLDER:
            self._enumerate(drive, docs, item["id"], rel_here, out, stats)
            return

        if mime == NATIVE_DOC:
            self._materialize_doc(drive, docs, item, rel_here, out, stats)
            return

        if mime in EXPORT_MAP:
            suffix, export_mime = EXPORT_MAP[mime]
            url = item.get("webViewLink") or _drive_view_url(item["id"], mime)
            file_id = item["id"]

            def _producer(dest: Path, _id: str = file_id, _m: str = export_mime) -> None:
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

        def _bin_producer(dest: Path, _id: str = file_id) -> None:
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
        out: list[_RemoteEntry],
        stats: GoogleDriveSyncStats,
    ) -> None:
        """Decide between a single ``.docx`` and one ``.md`` per tab.

        The Docs API gives us the full structure in one call, so we render
        all tabs from that single response — no extra round-trips per tab.
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
                        dest: Path, _b: str = body, _fp: str = fingerprint
                    ) -> None:
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
                    stats.tabs_written += 1
                return
            logger.info("doc %s reported tabs but none rendered", rel_here)

        # No tabs: export as docx, single file.
        def _docx_producer(dest: Path, _id: str = doc_id) -> None:
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


def _export_to(drive: Any, file_id: str, export_mime: str, dest: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    request = drive.files().export_media(fileId=file_id, mimeType=export_mime)
    with dest.open("wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def _download_to(drive: Any, file_id: str, dest: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with dest.open("wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def write_tab_with_fingerprint(dest: Path, fingerprint: str, body: str) -> None:
    """Write a tab markdown file with the inline fingerprint header.

    ``GoogleDriveConnector._is_unchanged`` reads the first line back to
    skip re-rendering tabs whose Docs ``modifiedTime + tabId`` haven't
    moved since the previous sync.
    """
    header = f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n"
    dest.write_text(header + body, encoding="utf-8")


def _drive_view_url(file_id: str, mime: str) -> str:
    """Fallback web URL when Drive omits ``webViewLink``."""
    if mime == NATIVE_DOC:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    if mime == NATIVE_SHEET:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if mime == NATIVE_SLIDES:
        return f"https://docs.google.com/presentation/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"
