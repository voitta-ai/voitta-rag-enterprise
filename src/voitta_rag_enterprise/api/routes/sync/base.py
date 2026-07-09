"""Shared plumbing for the sync route package.

Everything here is import-layer 0: no imports from sibling modules, so
any source module (github, google_drive, …) and the core envelope can
depend on it without cycles.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ....db.models import File, Folder
from ....services import events
from ....services.acl import CurrentUser, is_folder_owner, user_can_see_folder

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session

    from .core import SyncSourceOut

router = APIRouter(prefix="/folders/{folder_id}/sync", tags=["sync"])

# Separate router with no folder_id in the path — Google's OAuth callback
# URL is registered once with the Google client and cannot be parameterised.
oauth_router = APIRouter(prefix="/sync", tags=["sync"])


class SyncTriggerOut(BaseModel):
    folder_id: int
    job_id: int


def check_access(folder_id: int, db: Session, user: CurrentUser) -> Folder:
    """Read-level access check — for read-only endpoints (GET sync source)."""
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return folder


def check_owner(folder_id: int, db: Session, user: CurrentUser) -> Folder:
    """Owner-level access check — for any sync mutation.

    Sync configuration touches credentials, scheduling, and disk content;
    a read-only viewer of a shared folder must not be able to retrigger
    syncs or rotate auth.
    """
    folder = check_access(folder_id, db, user)
    if not is_folder_owner(db, folder_id, user.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the folder owner can configure sync.",
        )
    return folder


def folder_has_real_files(db: Session, folder_id: int, folder_root: Path) -> bool:
    """Empty = no indexed files AND no on-disk files (other than sync sidecars)."""
    from sqlalchemy import select

    has_db_files = (
        db.execute(
            select(File.id).where(
                File.folder_id == folder_id, File.state != "deleted"
            ).limit(1)
        ).first()
        is not None
    )
    if has_db_files:
        return True
    if not folder_root.exists():
        return False
    for entry in folder_root.iterdir():
        if entry.name in (".voitta_sources.json", ".voitta_timestamps.json"):
            continue
        return True
    return False


def external_redirect_uri(
    request: Request, callback_path: str, *, loopback_uri: str | None = None
) -> str:
    """Build an OAuth redirect URI the way the provider's consent screen
    will see it.

    When ``loopback_uri`` is given (per-folder opt-in) we return that fixed
    localhost URL — the admin has registered it with the provider and runs
    a small local nginx bridge that proxies the callback back here.

    Otherwise: ``request.base_url`` reflects what the ASGI server received
    on the wire, which is plain HTTP when a reverse proxy (Cloudflare,
    Caddy, nginx) terminates TLS in front of the app. The provider then
    rejects the code-exchange with a redirect-URI mismatch because the
    registered value is ``https://…``. Read ``X-Forwarded-Proto`` and
    ``X-Forwarded-Host`` ourselves so this works without requiring uvicorn
    to be launched with ``--proxy-headers``. Falls back to ``request.url``
    for the localhost / no-proxy case.
    """
    if loopback_uri:
        return loopback_uri
    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    proto = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{proto}://{host}{callback_path}"


def publish_sync_config_changed(folder_id: int, out: SyncSourceOut | None) -> None:
    """Push the full (secret-masked) config so an open sync modal in any tab
    reflects the save live, with no post-save refetch. Secrets are already
    reduced to has_* booleans by ``core.to_out``.

    EVERY route that creates or mutates a FolderSyncSource row must call
    this — the SPA caches the config per folder and trusts that cache on
    dialog open, so a missed publish leaves the dialog rendering a stale
    (possibly empty) config until a full page reload. ``out=None`` tells
    the client to drop its cached config for the folder.
    """
    events.publish(
        "folders",
        {
            "type": "folder.sync_config_changed",
            "folder_id": folder_id,
            "config": out.model_dump() if out is not None else None,
        },
    )


def publish_folder_changed(folder: Folder, *, has_sync_source: bool) -> None:
    events.publish(
        "folders",
        {
            "type": "folder.upserted",
            "folder": {
                "id": folder.id,
                "path": folder.path,
                "display_name": folder.display_name,
                "source_type": folder.source_type,
                "enabled": folder.enabled,
                "created_at": folder.created_at,
                "has_sync_source": has_sync_source,
            },
        },
    )


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_since(value: str) -> str | None:
    """Validate a YYYY-MM-DD recency-floor date from the UI (Jira/Confluence).

    Returns the trimmed string, or None when blank (→ connector default). The
    value is inlined into JQL/CQL, so a strict format check both guards against
    injection and catches typos before they hit the API.
    """
    v = (value or "").strip()
    if not v:
        return None
    if not _DATE_RE.match(v):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Recency floor must be a date in YYYY-MM-DD form.",
        )
    return v
