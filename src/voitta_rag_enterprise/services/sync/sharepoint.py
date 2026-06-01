"""SharePoint sync connector.

Mirrors selected SharePoint sites onto disk under a folder root. Each
site becomes its own subdirectory (``sites/<safe-name>/``) so a folder
with three sites picks up three sibling subtrees, the same layout the
legacy ``voitta-rag`` connector used.

Per site we materialise three things:

* **Drive items** — every file in the site's default document library,
  downloaded as-is. Office files (.docx/.xlsx/.pptx) flow into the
  indexer's normal extractor pipeline; native SharePoint blobs we
  don't have an extractor for (e.g. .one notebook stubs) are picked
  up by the OneNote exporter below.
* **SharePoint Pages** — modern .aspx pages → markdown under
  ``Pages/`` (see :mod:`microsoft_exporters.sharepoint_pages`).
* **OneNote notebooks** scoped to the site →
  ``OneNote/<notebook>/<section>/<page>.md``.

The connector writes ``.voitta_sources.json`` + ``.voitta_timestamps.json``
sidecars in the folder root (one merged sidecar across all sites).

Auth is delegated through :mod:`services.sync.microsoft_auth`; this
module is shape-agnostic about OAuth vs app-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from . import microsoft_auth as msa
from .base import SyncConnector
from .microsoft_exporters import (
    RemoteEntry,
    atomic_write_bytes,
    atomic_write_text,
    fingerprint_matches,
    safe_filename,
)
from .microsoft_exporters import onenote, sharepoint_pages

logger = logging.getLogger(__name__)


GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Sites field (de)serialisation
# ---------------------------------------------------------------------------


def coerce_sites_field(raw: str | None) -> list[dict[str, str]]:
    """Decode the JSON array stored in ``sp_selected_sites``.

    Expected: ``[{"id": "...", "displayName": "...", "webUrl": "..."}]``.
    Tolerates missing keys; ignores non-dict entries.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for entry in parsed:
        if isinstance(entry, dict) and entry.get("id"):
            out.append(
                {
                    "id": str(entry["id"]),
                    "displayName": str(entry.get("displayName") or entry.get("name") or ""),
                    "webUrl": str(entry.get("webUrl") or ""),
                }
            )
    return out


def encode_sites_field(sites: list[dict[str, str]] | None) -> str | None:
    if not sites:
        return None
    cleaned = [
        {
            "id": str(s["id"]),
            "displayName": str(s.get("displayName") or ""),
            "webUrl": str(s.get("webUrl") or ""),
        }
        for s in sites
        if isinstance(s, dict) and s.get("id")
    ]
    return json.dumps(cleaned) if cleaned else None


# ---------------------------------------------------------------------------
# URL parser (kept from legacy — handles the common SharePoint URL shapes)
# ---------------------------------------------------------------------------


def parse_sharepoint_url(url: str) -> tuple[str, str, str]:
    """Parse ``https://tenant.sharepoint.com/sites/Foo/...`` into
    ``(hostname, site_path, drive_subpath)``."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    full_path = unquote(parsed.path or "").rstrip("/")
    site_match = re.match(r"(/(?:sites|teams)/[^/]+)", full_path)
    if site_match:
        site_path = site_match.group(1)
        remainder = full_path[len(site_path):].lstrip("/")
    else:
        site_path = ""
        remainder = full_path.lstrip("/")
    drive_path = ""
    if remainder:
        remainder = re.sub(r"/Forms/[^/]*\.aspx$", "", remainder).rstrip("/")
        parts = remainder.split("/")
        if len(parts) > 1:
            drive_path = "/".join(parts[1:])
    return hostname, site_path, drive_path


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class SharePointSyncStats:
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    pages_written: int = 0
    notes_written: int = 0
    sites_synced: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
            "files_skipped": self.files_skipped,
            "pages_written": self.pages_written,
            "notes_written": self.notes_written,
            "sites_synced": self.sites_synced,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


async def _mint_token(auth: msa.MicrosoftAuth) -> str:
    if auth.method == "oauth":
        return await msa.refresh_access_token(auth)
    return await msa.get_app_only_token(auth)


# ---------------------------------------------------------------------------
# Sites listing — called from the API route's "pick sites" picker.
# ---------------------------------------------------------------------------


async def list_all_sites(auth: msa.MicrosoftAuth) -> list[dict[str, str]]:
    """Return every site the credentials can see ``{id, displayName, webUrl}``."""
    token = await _mint_token(auth)
    sites: list[dict[str, str]] = []
    url: str | None = f"{GRAPH_BASE}/sites?search=*&$select=id,displayName,name,webUrl"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await msa.graph_get(client, url, token)
            if resp.status_code != 200:
                msa.raise_graph_error(resp, "list sites")
            data = resp.json()
            for s in data.get("value", []):
                sites.append(
                    {
                        "id": s["id"],
                        "displayName": s.get("displayName") or s.get("name") or "",
                        "webUrl": s.get("webUrl") or "",
                    }
                )
            url = data.get("@odata.nextLink")
    sites.sort(key=lambda s: (s["displayName"] or "").lower())
    if len(sites) > 500:
        logger.warning(
            "Tenant exposes %d sites — 'all sites' sync will be slow",
            len(sites),
        )
    return sites


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class SharePointConnector(SyncConnector):
    """Sync selected SharePoint sites into ``folder_root``."""

    source_type = "sharepoint"
    supports_progress = True

    def resolve_config(self, row) -> dict:
        return {
            "auth": msa.MicrosoftAuth(
                tenant_id=row.ms_tenant_id or "",
                client_id=row.ms_client_id or "",
                client_secret=row.ms_client_secret or "",
                cert_pem=row.ms_cert_pem or "",
                refresh_token=row.ms_refresh_token or "",
                method=row.ms_auth_method or "",
            ),
            "sites": coerce_sites_field(row.sp_selected_sites),
            "all_sites": bool(row.sp_all_sites),
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: msa.MicrosoftAuth,
        sites: list[dict[str, str]],
        all_sites: bool,
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> SharePointSyncStats:
        if not auth.configured:
            raise RuntimeError(
                "SharePoint not connected. Open the folder's sync settings "
                "and click Connect, or configure an app-only credential."
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        def emit(phase: str, done: int, total: int, **detail: Any) -> None:
            if progress_cb is None:
                return
            progress_cb(phase, done, total, detail or None)

        emit("connecting", 0, 0)
        token = await _mint_token(auth)

        if all_sites:
            sites_to_sync = await list_all_sites(auth)
        else:
            sites_to_sync = list(sites or [])
        if not sites_to_sync:
            raise RuntimeError(
                "No SharePoint sites selected. Pick at least one site, "
                "or enable 'all sites'."
            )

        stats = SharePointSyncStats()
        sites_root = folder_root / "sites"
        sites_root.mkdir(parents=True, exist_ok=True)
        sidecar_sources: dict[str, dict[str, str]] = {}
        sidecar_times: dict[str, dict[str, str]] = {}
        synced_dirnames: set[str] = set()

        async with httpx.AsyncClient(timeout=60) as client:
            total = len(sites_to_sync)
            for idx, site in enumerate(sites_to_sync):
                emit(
                    "listing", idx, total,
                    current_site=site.get("displayName") or site.get("id"),
                )
                site_dir_name = safe_filename(
                    site.get("displayName") or site.get("id") or "site", "site"
                )
                synced_dirnames.add(site_dir_name)
                site_root = sites_root / site_dir_name
                site_root.mkdir(parents=True, exist_ok=True)
                try:
                    await self._sync_one_site(
                        client=client,
                        token=token,
                        site=site,
                        site_root=site_root,
                        folder_root=folder_root,
                        site_dir_name=site_dir_name,
                        sidecar_sources=sidecar_sources,
                        sidecar_times=sidecar_times,
                        stats=stats,
                    )
                    stats.sites_synced += 1
                except Exception as e:  # noqa: BLE001
                    logger.exception("Site sync failed: %s", site)
                    stats.errors.append(
                        f"site {site.get('displayName') or site.get('id')}: {e}"
                    )

        # Cleanup: drop any site directory that's no longer selected.
        if sites_root.exists():
            for child in list(sites_root.iterdir()):
                if child.is_dir() and child.name not in synced_dirnames:
                    logger.info("Removing stale site folder: %s", child.name)
                    shutil.rmtree(child, ignore_errors=True)

        # Write sidecars at the folder root — same convention as gdrive.
        (folder_root / ".voitta_sources.json").write_text(
            json.dumps(sidecar_sources, indent=2, sort_keys=True)
        )
        if sidecar_times:
            (folder_root / ".voitta_timestamps.json").write_text(
                json.dumps(sidecar_times, indent=2, sort_keys=True)
            )

        emit("done", stats.sites_synced, stats.sites_synced)
        return stats

    # ------------------------------------------------------------------
    # Per-site flow
    # ------------------------------------------------------------------

    async def _sync_one_site(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        site: dict[str, str],
        site_root: Path,
        folder_root: Path,
        site_dir_name: str,
        sidecar_sources: dict[str, dict[str, str]],
        sidecar_times: dict[str, dict[str, str]],
        stats: SharePointSyncStats,
    ) -> None:
        site_id = site["id"]
        # Find the site's default document library.
        drive_resp = await msa.graph_get(
            client, f"{GRAPH_BASE}/sites/{site_id}/drive", token
        )
        if drive_resp.status_code != 200:
            msa.raise_graph_error(drive_resp, f"drive lookup for site {site_id}")
        drive_id = drive_resp.json()["id"]

        # Phase 1 — drive items.
        drive_entries: list[_DriveItem] = []
        await self._list_drive_recursive(
            client=client,
            token=token,
            drive_id=drive_id,
            path="",
            items=drive_entries,
        )
        await self._download_drive_items(
            client=client,
            token=token,
            drive_id=drive_id,
            items=drive_entries,
            site_root=site_root,
            stats=stats,
        )

        # Phase 2 — Pages + OneNote (graceful 403 → skip).
        page_entries = await sharepoint_pages.export_site_pages(
            client=client,
            token=token,
            site_id=site_id,
            base_rel="Pages",
        )
        for entry in page_entries:
            self._materialise_text_entry(
                entry, site_root=site_root, stats=stats, kind="page"
            )
        stats.pages_written += len(page_entries)

        note_entries = await onenote.export_notebooks(
            client=client,
            token=token,
            owner_url=f"/sites/{site_id}",
            base_rel="OneNote",
        )
        for entry in note_entries:
            self._materialise_text_entry(
                entry, site_root=site_root, stats=stats, kind="note"
            )
        stats.notes_written += len(note_entries)

        # Phase 3 — mirror-delete files in this site dir that aren't in the
        # union of drive_items + page/note rel paths.
        expected: set[str] = set()
        for di in drive_entries:
            expected.add(di.rel_path)
        for entry in page_entries:
            expected.add(entry.rel_path)
        for entry in note_entries:
            expected.add(entry.rel_path)
        self._mirror_delete(site_root, expected, stats)

        # Phase 4 — record this site's sources into the global sidecar.
        for di in drive_entries:
            key = f"sites/{site_dir_name}/{di.rel_path}"
            sidecar_sources[key] = {
                "source": "sharepoint",
                "site_id": site_id,
                "site": site.get("displayName") or "",
                "url": di.web_url,
            }
            if di.modified_at or di.created_at:
                sidecar_times[key] = {
                    k: v
                    for k, v in (
                        ("modified_at", di.modified_at),
                        ("created_at", di.created_at),
                    )
                    if v
                }
        for entry in page_entries + note_entries:
            key = f"sites/{site_dir_name}/{entry.rel_path}"
            sidecar_sources[key] = {
                "source": "sharepoint",
                "site_id": site_id,
                "site": site.get("displayName") or "",
                "url": entry.url,
            }

    # ------------------------------------------------------------------
    # Drive listing + download
    # ------------------------------------------------------------------

    async def _list_drive_recursive(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        drive_id: str,
        path: str,
        items: list[_DriveItem],
    ) -> None:
        if not path:
            url: str | None = f"{GRAPH_BASE}/drives/{drive_id}/root/children"
        else:
            url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children"
        while url:
            resp = await msa.graph_get(client, url, token)
            if resp.status_code != 200:
                msa.raise_graph_error(resp, f"list drive children at '{path or 'root'}'")
            data = resp.json()
            for item in data.get("value", []):
                item_name = item.get("name", "")
                item_path = f"{path}/{item_name}" if path else item_name
                if "folder" in item:
                    await self._list_drive_recursive(
                        client=client,
                        token=token,
                        drive_id=drive_id,
                        path=item_path,
                        items=items,
                    )
                elif "file" in item:
                    items.append(
                        _DriveItem(
                            rel_path=item_path,
                            size=int(item.get("size") or 0),
                            modified_at=item.get("lastModifiedDateTime") or "",
                            created_at=item.get("createdDateTime") or "",
                            web_url=item.get("webUrl") or "",
                            sha256=(
                                (item.get("file") or {})
                                .get("hashes", {})
                                .get("sha256Hash")
                                or ""
                            ),
                            item_id=item["id"],
                        )
                    )
            url = data.get("@odata.nextLink")

    async def _download_drive_items(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        drive_id: str,
        items: list[_DriveItem],
        site_root: Path,
        stats: SharePointSyncStats,
    ) -> None:
        for di in items:
            local = site_root / di.rel_path
            if self._already_current(local, di):
                stats.files_skipped += 1
                continue
            existed = local.exists()
            try:
                await self._download_one(
                    client=client,
                    token=token,
                    drive_id=drive_id,
                    rel_path=di.rel_path,
                    local=local,
                )
                if existed:
                    stats.files_updated += 1
                else:
                    stats.files_added += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to download %s: %s", di.rel_path, e
                )
                stats.errors.append(f"download {di.rel_path}: {e}")

    @staticmethod
    def _already_current(local: Path, di: _DriveItem) -> bool:
        if not local.exists():
            return False
        if di.sha256:
            try:
                local_hash = hashlib.sha256(local.read_bytes()).hexdigest()
            except OSError:
                return False
            # Graph's sha256Hash is uppercase hex.
            return local_hash.lower() == di.sha256.lower()
        return local.stat().st_size == di.size

    async def _download_one(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        drive_id: str,
        rel_path: str,
        local: Path,
    ) -> None:
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{rel_path}:/content"
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            msa.raise_graph_error(resp, f"download '{rel_path}'")
        atomic_write_bytes(local, resp.content)

    # ------------------------------------------------------------------
    # Text-entry writer (used by Pages + OneNote)
    # ------------------------------------------------------------------

    def _materialise_text_entry(
        self,
        entry: RemoteEntry,
        *,
        site_root: Path,
        stats: SharePointSyncStats,
        kind: str,
    ) -> None:
        local = site_root / entry.rel_path
        if fingerprint_matches(local, entry.fingerprint):
            stats.files_skipped += 1
            return
        existed = local.exists()
        try:
            atomic_write_text(local, entry.payload or "", mtime=entry.mtime)
            if existed:
                stats.files_updated += 1
            else:
                stats.files_added += 1
        except OSError as e:
            stats.errors.append(f"{kind} write {entry.rel_path}: {e}")

    # ------------------------------------------------------------------
    # Mirror-delete (per site)
    # ------------------------------------------------------------------

    @staticmethod
    def _mirror_delete(
        site_root: Path, expected: set[str], stats: SharePointSyncStats
    ) -> None:
        for path in list(site_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(site_root).as_posix()
            if rel in expected:
                continue
            try:
                path.unlink()
                stats.files_removed += 1
            except OSError as e:
                stats.errors.append(f"unlink {rel}: {e}")
        for d in sorted(site_root.rglob("*"), reverse=True):
            if d.is_dir():
                with suppress(OSError):
                    d.rmdir()


# ---------------------------------------------------------------------------
# Internal drive-item record
# ---------------------------------------------------------------------------


@dataclass
class _DriveItem:
    rel_path: str
    size: int
    modified_at: str
    created_at: str
    web_url: str
    sha256: str
    item_id: str
