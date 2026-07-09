"""Google Drive sync connector.

Mirrors a Drive folder (My Drive subfolder, Shared Drive root, or
shared-with-me folder) onto disk under the registered folder root. The
watcher then picks up new/changed/deleted files and the indexing pipeline
takes over — same contract as the GitHub connector.

Native Google types are routed through the
:mod:`google_workspace_exporters` registry: each exporter is responsible
for converting one Drive file into one or more on-disk files plus a
sidecar deep-link. The registry's MIME map is the only place that
knows about specific Workspace types — adding Notion / Confluence /
etc. is a new exporter module + one line in
``build_default_registry``.

Cross-cutting:

* Tab markdown / sheet markdown / slide markdown / form markdown are
  written with a leading ``<!--voitta-fingerprint:...-->`` line so the
  next sync can short-circuit unchanged outputs without re-fetching.
* Inline images (Docs) and slide thumbnails (Slides) are written as
  separate :class:`RemoteEntry` objects with their own fingerprints so
  re-syncs only re-download bytes that actually changed.
* The full Sheets workbook is exported to
  ``.voitta_workbooks/<rel>.xlsx`` — that dir is excluded from indexing
  (see ``ignore_patterns`` default) and retrievable on demand via the
  ``voitta_rag_get_workbook`` MCP tool.
* ``vnd.google-apps.*`` types with no registered exporter (Sites, Apps
  Script, Fusion Tables, Jamboard) are skipped with a debug log — no
  useful textual export.

Authentication
--------------
Two modes, mirroring voitta-rag:

* **OAuth** — ``gd_client_id`` + ``gd_client_secret`` saved on the source
  row; the user clicks Connect, the unified callback (api/routes/sync.py)
  stores ``gd_refresh_token``. Access tokens are refreshed on demand and
  cached in-process. New scopes (``spreadsheets.readonly``,
  ``presentations.readonly``, ``forms.body.readonly``) ride alongside
  ``drive.readonly`` so Workspace exporters can call each app's API
  directly. Existing connections will need a one-time re-consent.
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

from .base import SyncConnector
from .google_workspace_exporters import (
    ExportContext,
    ProducerContext,
    RemoteEntry,
    get_default_registry,
    safe_filename,
)

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Native Workspace exporters need read access to each app's structural
# API: Docs for tab walking, Sheets for per-sheet markdown summaries,
# Slides for slide thumbnails + speaker notes, Forms for form
# structure. Asking for all of them up-front avoids a re-consent flow
# the moment a user's folder happens to contain a Sheet or a Slide.
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/documents.readonly "
    "https://www.googleapis.com/auth/spreadsheets.readonly "
    "https://www.googleapis.com/auth/presentations.readonly "
    "https://www.googleapis.com/auth/forms.body.readonly"
)

# Native Google types we know how to export. Anything else under
# ``vnd.google-apps.*`` is silently skipped.
NATIVE_DOC = "application/vnd.google-apps.document"
NATIVE_SHEET = "application/vnd.google-apps.spreadsheet"
NATIVE_SLIDES = "application/vnd.google-apps.presentation"
NATIVE_FOLDER = "application/vnd.google-apps.folder"

# Which Workspace API each native type's exporter depends on. Used to skip
# only the native types whose API is disabled in the OAuth client's GCP
# project (graceful degradation) instead of failing the whole sync or
# skipping every native type. Keys are the exporter ``mime_type``s
# (see google_workspace_exporters); values are attribute names on
# :class:`WorkspaceApiStatus`. Drawings export via the Drive API itself, so
# they only need ``drive`` (always required).
NATIVE_MIME_TO_API: dict[str, str] = {
    NATIVE_DOC: "docs",
    NATIVE_SHEET: "sheets",
    NATIVE_SLIDES: "slides",
    "application/vnd.google-apps.drawing": "drive",
    "application/vnd.google-apps.form": "forms",
}

# Sidecar dir for full Sheets workbook exports — also a member of the
# default ignore_patterns so the indexer never sees these files.
WORKBOOKS_DIR = ".voitta_workbooks"


class GoogleWorkspaceAccessError(RuntimeError):
    """Raised when one or more Workspace APIs aren't usable.

    Carries a list of ``(api_name, activation_url)`` tuples so the SPA
    can render an actionable message — typically the GCP "Enable API"
    URL Google itself returns inside the 403 body.

    ``scope_problem=True`` flags the distinct case where the OAuth
    token doesn't carry enough scopes (request returned 403 with
    ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` / ``insufficient_scope``).
    The fix in that case is reconnect, not "enable API in GCP".
    """

    def __init__(
        self,
        message: str,
        *,
        disabled_apis: list[tuple[str, str]] | None = None,
        scope_problem: bool = False,
    ) -> None:
        super().__init__(message)
        self.disabled_apis = disabled_apis or []
        self.scope_problem = scope_problem


@dataclass
class WorkspaceApiStatus:
    """Per-API result of probing the five Workspace APIs (non-raising).

    Each boolean is True when that API answered our bogus-id probe (i.e.
    it's enabled in the OAuth client's GCP project). ``disabled`` carries
    ``(label, activation_url)`` pairs for any API that returned 403
    SERVICE_DISABLED, so the UI can link straight to the GCP enable page.
    ``scope_problem`` flags the distinct "token missing scopes → reconnect"
    case (probing stops at the first such response).
    """

    drive: bool = False
    docs: bool = False
    sheets: bool = False
    slides: bool = False
    forms: bool = False
    scope_problem: bool = False
    disabled: list[tuple[str, str]] = field(default_factory=list)

    @property
    def native_ok(self) -> bool:
        """True when every native-export API (Docs/Sheets/Slides/Forms) is up."""
        return self.docs and self.sheets and self.slides and self.forms


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

    Expected shape: a JSON array of ``{"id": str, "name": str}`` objects,
    as produced by :func:`encode_folders_field`. Empty / None → ``[]``.
    """
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        return []
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
    # Files Drive listed but refused to serve at download time (404).
    # Common causes: file trashed / permission revoked / metadata stale
    # between ``files.list`` and ``files.get_media``. Tracked separately
    # from ``errors`` because the sync itself succeeded — these are
    # operational gaps in Drive, not failures the user can fix from
    # this modal. Logged once, surfaced in the log/stats line, and the
    # modal stays clean of them.
    files_404: int = 0
    tabs_written: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
            "files_skipped": self.files_skipped,
            "files_404": self.files_404,
            "tabs_written": self.tabs_written,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# OAuth helpers (callable from API routes — no connector instance needed)
# ---------------------------------------------------------------------------


def effective_oauth_client(row) -> tuple[str, str]:
    """The ``(client_id, client_secret)`` this source actually uses.

    ``gd_use_builtin`` rows get the deploy's built-in Desktop-app client
    (``VOITTA_GD_BUILTIN_CLIENT_ID/SECRET``, baked into the desktop
    build) — available only in single-user mode, where the OAuth consent
    redirect can reach this server on 127.0.0.1. Everything downstream
    (auth URL, code exchange, token refresh, cache keys) receives the
    substituted pair, so no other code knows the mode exists.
    """
    if getattr(row, "gd_use_builtin", False):
        from ...config import get_settings

        s = get_settings()
        if not (
            s.single_user and s.gd_builtin_client_id and s.gd_builtin_client_secret
        ):
            raise RuntimeError(
                "Built-in Google sign-in is not available on this deployment"
            )
        return s.gd_builtin_client_id, s.gd_builtin_client_secret
    return row.gd_client_id or "", row.gd_client_secret or ""


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


async def list_folder_children(
    *,
    parent_id: str,
    drive_id: str = "",
    client_id: str = "",
    client_secret: str = "",
    refresh_token: str = "",
    service_account_json: str = "",
) -> list[dict[str, str]]:
    """Return immediate subfolder children of *parent_id*.

    ``drive_id`` must be supplied when browsing inside a Shared Drive —
    the Drive API requires ``driveId`` + ``includeItemsFromAllDrives`` to
    reach those items.  For My Drive / Shared-with-me folders leave it
    empty.
    """
    if refresh_token:
        access_token = await _refresh_access_token(client_id, client_secret, refresh_token)
    elif service_account_json:
        access_token = await asyncio.to_thread(
            _service_account_access_token, service_account_json
        )
    else:
        raise RuntimeError("Cannot browse: no OAuth token and no service-account JSON")

    headers = {"Authorization": f"Bearer {access_token}"}
    base = "https://www.googleapis.com/drive/v3/files"
    fields = "files(id,name,owners(displayName,emailAddress),modifiedTime)"
    params: dict[str, str] = {
        "fields": fields,
        "pageSize": "200",
        "orderBy": "name",
        "q": (
            f"'{parent_id}' in parents "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        ),
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if drive_id:
        params["driveId"] = drive_id
        params["corpora"] = "drive"

    async with httpx.AsyncClient() as client:
        resp = await client.get(base, headers=headers, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"Drive browse failed: {resp.text[:300]}")
    return [_folder_summary(f) for f in resp.json().get("files", [])]


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


class GoogleDriveConnector(SyncConnector):
    """Mirror a Google Drive folder/Drive into ``folder_root``."""

    source_type = "google_drive"
    supports_progress = True

    # In-process cache: (client_id, refresh_token) → (access_token, expires_at).
    # Built-in-client rows resolve their creds BEFORE GoogleDriveAuth is
    # built (resolve_config), so client_id here is never blank for OAuth.
    _token_cache: ClassVar[dict[tuple[str, str], tuple[str, float]]] = {}

    def resolve_config(self, row) -> dict:
        client_id, client_secret = effective_oauth_client(row)
        return {
            "drive_folders": coerce_folders_field(row.gd_folder_id),
            "files_only": bool(row.gd_files_only),
            "auth": GoogleDriveAuth(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=row.gd_refresh_token or "",
                service_account_json=row.gd_service_account_json or "",
            ),
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: GoogleDriveAuth,
        drive_folders: list[dict[str, str]],
        files_only: bool = False,
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> GoogleDriveSyncStats:
        """Mirror Drive content into ``folder_root``.

        ``files_only=True`` downloads ordinary binary files (PDF, DOCX,
        images, …) and skips Google-native Docs/Sheets/Slides/Forms — only
        the Drive API needs to be enabled in that mode.

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
            files_only=files_only,
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

    # The following per-thread builders mirror ``_build_drive_for_thread``
    # for each Workspace API the exporters might need. Built lazily by
    # the per-thread factory in ``_sync_sync`` so a folder of pure plain
    # binaries doesn't pay for discovery on APIs nothing will use.

    def _build_sheets_for_thread(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> Any:
        from googleapiclient.discovery import build

        creds = self._build_credentials(auth, access_token)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    def _build_slides_for_thread(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> Any:
        from googleapiclient.discovery import build

        creds = self._build_credentials(auth, access_token)
        return build("slides", "v1", credentials=creds, cache_discovery=False)

    def _build_forms_for_thread(
        self, auth: GoogleDriveAuth, access_token: str | None
    ) -> Any:
        from googleapiclient.discovery import build

        creds = self._build_credentials(auth, access_token)
        return build("forms", "v1", credentials=creds, cache_discovery=False)

    def probe_apis(self, auth: GoogleDriveAuth) -> WorkspaceApiStatus:
        """Probe which Workspace APIs are enabled for ``auth`` (blocking).

        Builds the five services and runs :func:`probe_workspace_apis`.
        Never raises for a disabled/scope problem — that's encoded in the
        returned :class:`WorkspaceApiStatus`. Powers the sync modal's
        "Test API availability" button. Run via ``asyncio.to_thread``;
        the googleapiclient calls block.
        """
        access_token = self._sync_access_token(auth) if auth.refresh_token else None
        drive, docs = self._build_services(auth, access_token)
        sheets = self._build_sheets_for_thread(auth, access_token)
        slides = self._build_slides_for_thread(auth, access_token)
        forms = self._build_forms_for_thread(auth, access_token)
        return probe_workspace_apis(
            drive=drive, docs=docs, sheets=sheets, slides=slides, forms=forms
        )

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
        files_only: bool = False,
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

        # Decide which native Workspace types we can actually export, by
        # probing every API once (five cheap bogus-id calls) up front —
        # instead of discovering API-not-enabled mid-sync after listing
        # 50,000 files and fail-bursting the materialise pool with 403
        # SERVICE_DISABLED. Drive is always required (fatal if disabled or
        # the token lost its scope). For the rest we degrade gracefully:
        # export native types whose API is enabled, skip only those whose
        # API is off. ``files_only`` forces "skip every native type" (mirror
        # binaries only) regardless of which APIs are on.
        _emit("validating")
        enabled_native_mimes: frozenset[str]
        if files_only:
            preflight_drive_only(drive)
            enabled_native_mimes = frozenset()
        else:
            sheets_svc = self._build_sheets_for_thread(auth, access_token)
            slides_svc = self._build_slides_for_thread(auth, access_token)
            forms_svc = self._build_forms_for_thread(auth, access_token)
            status = probe_workspace_apis(
                drive=drive,
                docs=docs,
                sheets=sheets_svc,
                slides=slides_svc,
                forms=forms_svc,
            )
            # Drive disabled / missing scope is fatal — nothing can sync.
            # Re-run the Drive-only check so we raise the same actionable
            # error (with the GCP enable URL / reconnect hint).
            if not status.drive or status.scope_problem:
                preflight_drive_only(drive)
            enabled_native_mimes = frozenset(
                mime
                for mime, api in NATIVE_MIME_TO_API.items()
                if getattr(status, api, False)
            )
            skipped_types = sorted(
                set(NATIVE_MIME_TO_API) - enabled_native_mimes
            )
            if skipped_types:
                logger.info(
                    "native types skipped (Workspace API not enabled): %s",
                    ", ".join(skipped_types),
                )

        # Same ignore matcher the watcher uses, so policy is consistent
        # whether files arrive over Drive or via local fs. Matching here
        # also avoids the egress cost of downloading bytes we'd then
        # immediately ignore (a single mp4 can be hundreds of MB).
        from ..ignore import from_settings as _ignore_from_settings

        ignore = _ignore_from_settings()

        stats = GoogleDriveSyncStats()
        entries: list[RemoteEntry] = []
        # Native Workspace files (Docs/Sheets/Slides/Drawings/Forms)
        # need a per-file structural API call to materialise — collect
        # them during listing and fan out through a thread pool below
        # so the per-file round-trip doesn't dominate listing time.
        pending_native: list[tuple[dict[str, Any], str]] = []
        # Each picked Drive folder is mirrored under its own subdirectory so
        # multi-folder syncs can't collide on same-named children. The
        # subdirectory is the folder's display name (set by the picker UI),
        # with a stable fallback when it's missing.
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
            # "Shared by" for this synced root → downfilled onto every item
            # below. For a folder shared into this account (e.g. via a service
            # account) ``sharingUser`` names who shared it; otherwise fall back
            # to the folder's owner. Best-effort — never block the sync on it.
            self._shared_by = self._fetch_shared_by(drive, folder_id)
            tick = _make_listing_tick(idx, display)
            tick()  # entering this folder — flips current_folder in the badge
            try:
                self._enumerate(
                    drive, docs, folder_id, unique, entries, stats, ignore,
                    on_page=tick,
                    skipped_counter=skipped_during_listing,
                    pending_native=pending_native,
                    enabled_native_mimes=enabled_native_mimes,
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

        # Phase 2: fan out the per-file structural API calls accumulated
        # during listing. Every native Workspace type (Doc / Sheet /
        # Slides / Drawing / Form) needs at least one round-trip to
        # render, and they're embarrassingly parallel. Cap concurrency
        # at 4 to stay well under Drive's per-user RPS budget;
        # googleapiclient's Resource is NOT thread-safe — each pool
        # worker gets its own per-API services via the per-thread
        # builders below so concurrent requests don't share an
        # httplib2.Http and trip Drive's timeout / SSL-error responses.
        if pending_native:
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed

            n_native = len(pending_native)
            _emit(
                "fetching_docs",
                0,
                n_native,
                {"items_seen": len(entries)},
            )

            tl = threading.local()

            def _per_thread_factory(name: str, builder: Callable[..., Any]) -> Callable[[], Any]:
                """Produce a callable that returns a thread-local Resource.

                ``getattr(tl, name)`` lazily caches the per-API service
                on the worker thread the first time the exporter asks
                for it. Discovery cost (~50ms / API) thus pays off
                across every task the thread runs, not per-task.
                """

                def _get() -> Any:
                    cached = getattr(tl, name, None)
                    if cached is None:
                        cached = builder(auth, access_token)
                        setattr(tl, name, cached)
                    return cached

                return _get

            registry = get_default_registry()
            native_emit_every = max(1, n_native // 50)

            def _materialize_one(
                item: dict, rel_here: str
            ) -> tuple[str, list[RemoteEntry]]:
                exporter = registry.find(item["mimeType"])
                if exporter is None:
                    return rel_here, []
                ctx = ExportContext(
                    folder_root=folder_root,
                    docs=_per_thread_factory("docs", self._build_docs_for_thread),
                    sheets=_per_thread_factory("sheets", self._build_sheets_for_thread),
                    slides=_per_thread_factory("slides", self._build_slides_for_thread),
                    forms=_per_thread_factory("forms", self._build_forms_for_thread),
                    drive_thread_local=_per_thread_factory(
                        "drive", self._build_drive_for_thread
                    ),
                    access_token=access_token,
                )
                native_entries = exporter.export(item, rel_here, ctx)
                # Same per-item provenance as binary files — stamped onto every
                # entry the exporter produced (a multi-tab doc yields several).
                meta = self._drive_item_meta(item)
                for e in native_entries:
                    e.extra["meta"] = meta
                return rel_here, native_entries

            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="gd-native"
            ) as pool:
                futures = {
                    pool.submit(_materialize_one, item, rel_here): rel_here
                    for (item, rel_here) in pending_native
                }
                for completed, fut in enumerate(as_completed(futures), start=1):
                    rel = futures[fut]
                    try:
                        _, native_entries = fut.result()
                    except Exception as e:
                        logger.exception("Drive native materialize failed for %s", rel)
                        stats.errors.append(f"{rel}: {e}")
                    else:
                        entries.extend(native_entries)
                        # Per-tab markdown files contribute to tabs_written
                        # so the SPA's "tabs" stat keeps its meaning.
                        stats.tabs_written += sum(
                            1 for e in native_entries
                            if e.tab is not None and e.rel_path.endswith(".md")
                        )
                    if completed == n_native or completed % native_emit_every == 0:
                        _emit(
                            "fetching_docs",
                            completed,
                            n_native,
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

        def _per_thread_dl(name: str, builder: Callable[..., Any]) -> Callable[[], Any]:
            def _get() -> Any:
                cached = getattr(tl_dl, name, None)
                if cached is None:
                    cached = builder(auth, access_token)
                    setattr(tl_dl, name, cached)
                return cached

            return _get

        def _build_dl_producer_ctx() -> ProducerContext:
            """Producer context for the download phase.

            Producers (e.g. SpreadsheetExporter's per-sheet markdown
            writer, SlidesExporter's thumbnail fetcher) ask for the
            services they need; everything is lazy so a sync of pure
            plain binaries never hits the Sheets / Slides / Forms
            discovery endpoints.
            """
            return ProducerContext(
                folder_root=folder_root,
                docs=_per_thread_dl("docs", self._build_docs_for_thread),
                sheets=_per_thread_dl("sheets", self._build_sheets_for_thread),
                slides=_per_thread_dl("slides", self._build_slides_for_thread),
                forms=_per_thread_dl("forms", self._build_forms_for_thread),
                access_token=access_token,
            )

        def _materialize_one(
            entry: RemoteEntry,
        ) -> tuple[RemoteEntry, bool, bool, str | None, bool]:
            """Run the producer for one entry off the main thread.

            Returns ``(entry, was_unchanged, existed_before, error,
            is_404)``. We keep stats + sidecar mutation on the main
            thread (single-threaded as ``as_completed`` drains) to
            avoid a lock.

            ``is_404=True`` flags Drive's "we listed it, won't serve
            it" race so the caller can route to ``stats.files_404``
            instead of the user-facing ``stats.errors``. Common
            triggers: file trashed / unshared / metadata stale
            between ``files.list`` and ``files.get_media``.
            """
            local = folder_root / entry.rel_path
            local.parent.mkdir(parents=True, exist_ok=True)
            if self._is_unchanged(local, entry):
                return entry, True, False, None, False
            existed_before = local.exists()
            try:
                entry.producer(
                    local,
                    _per_thread_dl("drive", self._build_drive_for_thread)(),
                    _build_dl_producer_ctx(),
                )
            except Exception as e:
                if _is_drive_404(e):
                    # Don't pollute the modal's error block — log once
                    # at info level and bucket separately.
                    logger.info(
                        "Drive 404 (file vanished between list/get): %s",
                        entry.rel_path,
                    )
                    return entry, False, existed_before, None, True
                logger.exception("Drive download failed: %s", entry.rel_path)
                return entry, False, existed_before, str(e), False
            return entry, False, existed_before, None, False

        with ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="gd-dl"
        ) as pool:
            futures = [pool.submit(_materialize_one, e) for e in entries]
            for i, fut in enumerate(as_completed(futures), start=1):
                entry, was_unchanged, existed_before, err, is_404 = fut.result()
                if is_404:
                    stats.files_404 += 1
                elif err is not None:
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
        # Drop locals not on remote (skip our own sidecars). Files under
        # ``.voitta_workbooks/`` are added to ``expected_paths`` directly
        # by SpreadsheetExporter, so the orphan-detection pass treats
        # them like any other entry — no separate keep entry needed for
        # the dir itself.
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
        out: list[RemoteEntry],
        stats: GoogleDriveSyncStats,
        ignore: Any,  # IgnoreMatcher
        on_page: Callable[[], None] | None = None,
        skipped_counter: list[int] | None = None,
        pending_native: list[tuple[dict[str, Any], str]] | None = None,
        enabled_native_mimes: frozenset[str] = frozenset(),
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
                        "files(id,name,mimeType,size,modifiedTime,createdTime,"
                        "md5Checksum,webViewLink,"
                        "owners(displayName,emailAddress),"
                        "lastModifyingUser(displayName,emailAddress))"
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
                    pending_native=pending_native,
                    enabled_native_mimes=enabled_native_mimes,
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
        out: list[RemoteEntry],
        stats: GoogleDriveSyncStats,
        ignore: Any,  # IgnoreMatcher
        on_page: Callable[[], None] | None = None,
        skipped_counter: list[int] | None = None,
        pending_native: list[tuple[dict[str, Any], str]] | None = None,
        enabled_native_mimes: frozenset[str] = frozenset(),
    ) -> None:
        name = item["name"]
        mime = item["mimeType"]
        rel_here = f"{rel_prefix}/{name}" if rel_prefix else name

        # Stamp the current synced-root's "shared by" onto the item now, while
        # self._shared_by is correct for this folder. Native exports are
        # materialized later in a cross-folder batch, so they read it from here.
        item["_voitta_shared_by"] = getattr(self, "_shared_by", {}) or {}

        registry = get_default_registry()
        is_native_known = registry.find(mime) is not None

        # Native Google type whose exporter API isn't available for this
        # sync (its Workspace API is disabled in the GCP project, or
        # files-only mode is on). Skip it gracefully — folders still
        # recurse, binary files still sync, and native types whose API
        # *is* enabled fall through to the exporter pool below.
        if (
            is_native_known
            and mime != NATIVE_FOLDER
            and mime not in enabled_native_mimes
        ):
            logger.debug("skipping native type (API unavailable): %s (%s)", mime, rel_here)
            stats.files_skipped += 1
            if skipped_counter is not None:
                skipped_counter[0] += 1
            return

        # Apply two skip layers BEFORE we recurse / produce:
        #   1. VOITTA_IGNORE_PATTERNS — filename globs (covers ``*.mp4``,
        #      ``node_modules``, …); the matcher walks ancestors too, so a
        #      file inside an ignored directory is skipped without listing
        #      its contents.
        #   2. MIME blocklist — catches Drive items the filename globs miss
        #      (Meet recordings often have no extension or names like
        #      ``Recording 2026-04-12``; Drive still tags them ``video/mp4``).
        # Native Google types we have an exporter for stay un-skippable —
        # they get rendered into per-file markdown the parsers handle.
        skippable = mime != NATIVE_FOLDER and not is_native_known
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
                pending_native=pending_native,
                enabled_native_mimes=enabled_native_mimes,
            )
            return

        if is_native_known:
            # Native Workspace types (Doc / Sheet / Slides / Drawing /
            # Form) need a structural API call to materialise — that's
            # one round-trip per file, embarrassingly parallel, so we
            # defer to the thread pool below.
            if pending_native is not None:
                pending_native.append((item, rel_here))
            return

        if mime.startswith("application/vnd.google-apps."):
            # A native type with no registered exporter (Sites, Apps
            # Script, Fusion Tables, Jamboard, ...). Drop quietly —
            # there's no useful textual export.
            logger.debug("skipping unsupported google-native type: %s (%s)", mime, name)
            return

        # Plain binary file.
        url = item.get("webViewLink") or _drive_view_url(item["id"], mime)
        size = int(item.get("size") or 0)
        file_id = item["id"]

        def _bin_producer(
            dest: Path,
            drive: Any,
            ctx: ProducerContext,  # noqa: ARG001
            _id: str = file_id,
        ) -> None:
            _download_to(drive, _id, dest)

        out.append(
            RemoteEntry(
                rel_path=rel_here,
                url=url,
                tab=None,
                producer=_bin_producer,
                fingerprint=item.get("md5Checksum") or item.get("modifiedTime", ""),
                size_hint=size,
                extra={"meta": self._drive_item_meta(item)},
            )
        )

    # -- diffing helpers ----------------------------------------------------

    def _is_unchanged(self, local: Path, entry: RemoteEntry) -> bool:
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

    def _fetch_shared_by(self, drive: Any, folder_id: str) -> dict[str, str]:
        """Resolve who shared the synced root folder → ``{name, email}`` or {}.

        ``sharingUser`` when the folder was shared into this account; else the
        folder's owner. Best-effort: any API hiccup yields {} (no shared_by).
        """
        try:
            meta = drive.files().get(
                fileId=folder_id,
                fields="sharingUser(displayName,emailAddress),"
                       "owners(displayName,emailAddress),ownedByMe",
                supportsAllDrives=True,
            ).execute()
        except Exception:
            return {}
        u = meta.get("sharingUser")
        if not u and not meta.get("ownedByMe"):
            owners = meta.get("owners") or []
            u = owners[0] if owners else None
        if not u:
            return {}
        return {"name": u.get("displayName") or "", "email": u.get("emailAddress") or ""}

    def _drive_item_meta(self, item: dict) -> dict:
        """Build the source_meta dict for one Drive item (owner/editor/dates +
        downfilled shared_by).

        ``shared_by`` is read from the item itself (stamped at enumerate time in
        ``_process_item``) rather than ``self._shared_by`` — native exports are
        materialized in a deferred batch *after* all folders are enumerated, so
        the live ``self._shared_by`` would be the last folder's value. Reading
        the per-item stamp keeps multi-folder syncs correct.
        """
        from .. import source_meta as sm

        owners = item.get("owners") or []
        owner = owners[0] if owners else {}
        editor = item.get("lastModifyingUser") or {}
        shared = item.get("_voitta_shared_by") or getattr(self, "_shared_by", {}) or {}
        return sm.build(
            owner_name=owner.get("displayName"),
            owner_email=owner.get("emailAddress"),
            editor_name=editor.get("displayName"),
            editor_email=editor.get("emailAddress"),
            shared_by_name=shared.get("name"),
            shared_by_email=shared.get("email"),
            created=item.get("createdTime"),
            modified=item.get("modifiedTime"),
        )

    def _record_sidecar(
        self, sidecar: dict[str, dict[str, str]], entry: RemoteEntry
    ) -> None:
        record: dict[str, Any] = {"url": entry.url}
        if entry.tab:
            record["tab"] = entry.tab
        # Source provenance captured at listing time (see _drive_item_meta).
        meta = entry.extra.get("meta") if entry.extra else None
        if meta:
            record.update(meta)
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


def _is_drive_404(error: BaseException) -> bool:
    """True iff ``error`` is a Drive HttpError with status 404.

    Used to bucket "Drive listed the file but won't serve its bytes"
    races into ``stats.files_404`` so they don't pollute the user-
    facing ``stats.errors`` block. We accept the exception by
    duck-type rather than ``isinstance(HttpError, …)`` because
    googleapiclient is heavy to import on cold paths and the
    ``resp.status`` attribute is the only thing we read.
    """
    resp = getattr(error, "resp", None)
    status_code = getattr(resp, "status", None)
    return status_code == 404


def _download_to(drive: Any, file_id: str, dest: Path) -> None:
    _atomic_stream_download(
        lambda: drive.files().get_media(fileId=file_id, supportsAllDrives=True),
        dest,
    )


# A bogus id Google's APIs reject with 404 NOT_FOUND when the API is
# enabled (the most informative "API works" signal you can get without
# side-effects) and 403 SERVICE_DISABLED when the API isn't enabled in
# the project. The string is illegal under every Workspace ID format
# (UUIDs, file ids), so we won't accidentally hit a real resource.
_PREFLIGHT_BOGUS_ID = "voitta-preflight-bogus-id-0000"


# Probe definitions: ``(human_label, build_request_callable)``. The
# callable takes the connector instance + ExportContext and returns
# the request (so it builds the per-API service the same way the rest
# of the connector does). API-name strings match what Google uses in
# its activation URL: ``docs.googleapis.com``, etc.
_PREFLIGHT_PROBES: tuple[tuple[str, str, str], ...] = (
    ("Google Drive", "drive.googleapis.com", "drive"),
    ("Google Docs", "docs.googleapis.com", "docs"),
    ("Google Sheets", "sheets.googleapis.com", "sheets"),
    ("Google Slides", "slides.googleapis.com", "slides"),
    ("Google Forms", "forms.googleapis.com", "forms"),
)


def _is_service_disabled(error: Any) -> bool:
    """True iff the googleapiclient ``HttpError`` indicates the API is
    not enabled in the GCP project. Google returns 403 with
    ``reason=SERVICE_DISABLED`` and an ``activationUrl`` pointing at
    the API library page.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return False
    if not isinstance(error, HttpError):
        return False
    if error.resp.status != 403:
        return False
    body = (error.content or b"").decode("utf-8", errors="replace")
    return "SERVICE_DISABLED" in body


def _is_scope_insufficient(error: Any) -> bool:
    """True iff the error is a 401/403 caused by missing OAuth scopes.

    Google's various code paths return either:
    * 401 ``invalid_token`` with ``error_description=insufficient_scope``,
    * 403 ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` (the modern code), or
    * 403 ``insufficientPermissions`` (legacy on some Drive endpoints).
    All three mean the same thing: re-consent OAuth.
    """
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return False
    if not isinstance(error, HttpError):
        return False
    if error.resp.status not in (401, 403):
        return False
    body = (error.content or b"").decode("utf-8", errors="replace")
    return (
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body
        or "insufficient_scope" in body
        or "insufficientPermissions" in body
    )


def _extract_activation_url(api_name: str, error: Any) -> str:
    """Pull Google's ``activationUrl`` out of the 403 body when present;
    otherwise build the standard ``console.cloud.google.com/apis/library``
    URL by hand. Either way, the returned URL is the one-click "enable
    this API" page for the project."""
    import json as _json

    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return f"https://console.cloud.google.com/apis/library/{api_name}"
    if isinstance(error, HttpError) and error.content:
        try:
            body = _json.loads(error.content.decode("utf-8", errors="replace"))
            details = (body.get("error") or {}).get("details") or []
            for d in details:
                url = (d.get("metadata") or {}).get("activationUrl")
                if url:
                    return url
        except (ValueError, AttributeError):
            pass
    return f"https://console.cloud.google.com/apis/library/{api_name}"


def _run_preflight_probe(service: Any, api_id: str) -> Any:
    """Issue one bogus-id call against ``service`` for API ``api_id``.

    Returns the ``HttpError`` if the call failed, or ``None`` on
    unexpected success (the bogus id should never resolve to a real
    resource — but defensively we return None and the caller treats
    that as "API works").
    """
    from googleapiclient.errors import HttpError

    try:
        if api_id == "drive":
            service.files().get(
                fileId=_PREFLIGHT_BOGUS_ID, supportsAllDrives=True
            ).execute()
        elif api_id == "docs":
            service.documents().get(documentId=_PREFLIGHT_BOGUS_ID).execute()
        elif api_id == "sheets":
            service.spreadsheets().get(spreadsheetId=_PREFLIGHT_BOGUS_ID).execute()
        elif api_id == "slides":
            service.presentations().get(presentationId=_PREFLIGHT_BOGUS_ID).execute()
        elif api_id == "forms":
            service.forms().get(formId=_PREFLIGHT_BOGUS_ID).execute()
        else:
            raise ValueError(f"unknown api id {api_id!r}")
        return None
    except HttpError as e:
        return e


def probe_workspace_apis(
    *,
    drive: Any,
    docs: Any,
    sheets: Any,
    slides: Any,
    forms: Any,
) -> WorkspaceApiStatus:
    """Probe every Workspace API once with a deliberately bogus id.

    Each enabled API returns 404 NOT_FOUND for an unknown resource;
    each *disabled* API returns 403 SERVICE_DISABLED. We classify each
    probe and return a :class:`WorkspaceApiStatus` — this never raises,
    so callers can either render the result (the UI "Test API
    availability" button) or enforce a policy (:func:`preflight_workspace_apis`).

    The current OAuth scope set covers all five APIs at once, so a
    scope-insufficient response means the refresh_token predates the
    scope expansion and the user needs to reconnect. We surface that
    distinctly and stop probing (re-consent fixes every later probe).

    Five Drive API round-trips; cheap (parallel-ish via underlying
    httplib2 keepalive) and the one-shot diagnostic is worth the cost vs.
    discovering API-not-enabled mid-sync after listing 50,000 files.
    """
    services = {
        "drive": drive,
        "docs": docs,
        "sheets": sheets,
        "slides": slides,
        "forms": forms,
    }
    status = WorkspaceApiStatus()
    for label, api_name, api_id in _PREFLIGHT_PROBES:
        err = _run_preflight_probe(services[api_id], api_id)
        if err is None:
            # Bogus id surprisingly resolved (impossible in practice).
            # Treat as "API enabled, move on".
            setattr(status, api_id, True)
            continue
        if _is_service_disabled(err):
            status.disabled.append((label, _extract_activation_url(api_name, err)))
            continue
        if _is_scope_insufficient(err):
            status.scope_problem = True
            # Stop probing — re-consent fixes every subsequent probe.
            break
        # Anything else (404 NOT_FOUND, 400 INVALID_ARGUMENT) means the
        # API responded — it's enabled, just rejected our bogus id.
        setattr(status, api_id, True)
    return status


def preflight_workspace_apis(
    *,
    drive: Any,
    docs: Any,
    sheets: Any,
    slides: Any,
    forms: Any,
) -> None:
    """All-or-nothing preflight: raise if any Workspace API is unusable.

    Thin policy wrapper over :func:`probe_workspace_apis`. Behaviour is
    unchanged from before the probe/preflight split — used by the default
    (non files-only) sync path so a missing Sheets/Slides/Forms API fails
    fast with an actionable, URL-carrying error instead of fail-bursting
    the materialise pool mid-sync.
    """
    status = probe_workspace_apis(
        drive=drive, docs=docs, sheets=sheets, slides=slides, forms=forms
    )
    _raise_for_status(status)


def preflight_drive_only(drive: Any) -> None:
    """Files-only preflight: require just the Drive API.

    Used when the folder is configured for files-only sync — we never
    touch Docs/Sheets/Slides/Forms, so only the Drive API must be enabled
    (and the token must carry at least the Drive scope). A missing Drive
    API is fatal: nothing can be listed or downloaded without it.
    """
    err = _run_preflight_probe(drive, "drive")
    if err is None:
        return
    if _is_scope_insufficient(err):
        raise GoogleWorkspaceAccessError(
            "OAuth token is missing the Drive scope. Reconnect Google "
            "Drive in the sync modal to re-grant access.",
            scope_problem=True,
        )
    if _is_service_disabled(err):
        url = _extract_activation_url("drive.googleapis.com", err)
        raise GoogleWorkspaceAccessError(
            "The Google Drive API is not enabled in the project this OAuth "
            f"client belongs to. Enable it, then retry sync:\n  • Google "
            f"Drive: {url}",
            disabled_apis=[("Google Drive", url)],
        )
    # Any other HttpError (404 for the bogus id, etc.) means Drive answered.


def _raise_for_status(status: WorkspaceApiStatus) -> None:
    """Raise :class:`GoogleWorkspaceAccessError` if ``status`` shows a problem."""
    if status.scope_problem:
        raise GoogleWorkspaceAccessError(
            "OAuth token is missing required scopes. Reconnect Google "
            "Drive in the sync modal to grant access to Docs / Sheets / "
            "Slides / Forms.",
            scope_problem=True,
        )
    if status.disabled:
        names = ", ".join(label for label, _ in status.disabled)
        details = "\n".join(f"  • {label}: {url}" for label, url in status.disabled)
        raise GoogleWorkspaceAccessError(
            f"The following Google Cloud APIs are not enabled in the "
            f"project this OAuth client belongs to: {names}. Enable each "
            f"in the GCP console, then retry sync:\n{details}",
            disabled_apis=status.disabled,
        )


def _drive_view_url(file_id: str, mime: str) -> str:
    """Fallback web URL when Drive omits ``webViewLink``."""
    if mime == NATIVE_DOC:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    if mime == NATIVE_SHEET:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    if mime == NATIVE_SLIDES:
        return f"https://docs.google.com/presentation/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"
