"""Folder registration + reconciliation endpoints."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import File, Folder, FolderSyncSource, Image, Job
from ...services import events, job_queue
from ...services.indexing import file_event_payload, publish_file_upserted
from ...services.acl import (
    CurrentUser,
    folder_active_for_user,
    grant_folder,
    is_folder_owner,
    revoke_folder,
    set_folder_active,
    user_can_see_folder,
    visible_folder_ids,
)
from ...services.scanner import scan_folder
from ...services.watcher import unwatch_folder_in_default, watch_folder_in_default
from ..deps import current_user, db_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/folders", tags=["folders"])


class FolderIn(BaseModel):
    """Create a folder under ``$VOITTA_ROOT_PATH``.

    Pass ``name`` (single path segment); the server creates
    ``$VOITTA_ROOT_PATH/<name>`` if missing. Sync connectors can later be
    attached. External-path registration was removed — see commit log.
    """

    name: str = Field(description="Folder name under VOITTA_ROOT_PATH")
    display_name: str | None = None


class FolderOut(BaseModel):
    id: int
    path: str
    display_name: str
    source_type: str
    enabled: bool
    created_at: int
    has_sync_source: bool = False
    # UI-facing data-source identifier. Folded down from the joined
    # FolderSyncSource row so the frontend doesn't need to know about
    # gh_auth_method / gd_client_id / etc. One of:
    #   "regular"         — no sync source
    #   "github_public"   — GitHub mirror with no credentials
    #   "github_private"  — GitHub mirror with SSH or token auth
    #   "google_drive"    — Drive folder sync
    # Future connectors append new values; the frontend falls back
    # to the "regular" upload-folder icon for anything unknown.
    sync_source_kind: str = "regular"
    # Live sync-source state ("idle" | "syncing" | "error"), folded down from
    # the FolderSyncSource row so the tree pill can show "syncing" from boot
    # truth; live transitions arrive as folder.sync_source_changed WS events.
    sync_status: str = "idle"
    # Ownership / sharing — see services/acl.py docstring.
    owner_id: int | None = None
    owned: bool = False  # True if the calling user owns this folder
    shared: bool = False  # True if owner has flipped the shared switch on
    # Per-user MCP-search opt-out (default-on, missing row = True).
    active: bool = True


class RootInfo(BaseModel):
    root_path: str | None
    configured: bool


class FileOut(BaseModel):
    id: int
    folder_id: int
    rel_path: str
    state: str
    size_bytes: int | None
    mtime_ns: int | None
    last_indexed_at: int | None
    pending_embeds: int
    source_url: str | None


def _sync_source_kind(source: FolderSyncSource | None) -> str:
    """Reduce the FolderSyncSource row to a single UI-facing string.

    ``regular`` covers both "no row" and any row whose ``source_type``
    we don't yet have a frontend icon for — the UI falls back to the
    generic upload-folder icon, so it's safe to treat as default.
    """
    if source is None:
        return "regular"
    if source.source_type == "github":
        # The repo URL is the only reliable signal:
        #   * ``git@host:org/repo`` or ``ssh://...``  — SSH URL, only
        #     works with auth (either a stored SSH key OR the host's
        #     own SSH agent / default key). Either way: private.
        #   * ``https://`` with a stored PAT — explicitly private.
        #   * everything else (plain ``https://github.com/...`` without
        #     a PAT) — anonymous clone, treat as public.
        # The previous heuristic looked at ``gh_auth_method`` which the
        # form populates by default ("ssh" radio selection), so it
        # lit up the lock on every github row.
        url = (source.gh_repo or "").strip()
        is_ssh_url = url.startswith("git@") or url.startswith("ssh://")
        has_https_pat = (
            url.startswith("https://")
            and source.gh_auth_method == "token"
            and bool(source.gh_pat)
        )
        return "github_private" if (is_ssh_url or has_https_pat) else "github_public"
    if source.source_type in ("google_drive", "google_drive_local"):
        # Both wear the Google Drive brand mark — same content origin, just
        # API-synced vs. indexed-in-place from the local Drive app.
        return "google_drive"
    if source.source_type == "nfs":
        return "nfs"
    if source.source_type == "sharepoint":
        return "sharepoint"
    if source.source_type == "teams":
        return "teams"
    if source.source_type == "jira":
        return "jira"
    if source.source_type == "confluence":
        return "confluence"
    return "regular"


def _to_folder_out(
    f: Folder,
    *,
    has_sync_source: bool = False,
    sync_source_kind: str = "regular",
    sync_status: str = "idle",
    owned: bool = False,
    active: bool = True,
) -> FolderOut:
    return FolderOut(
        id=f.id,
        path=f.path,
        display_name=f.display_name,
        source_type=f.source_type,
        enabled=f.enabled,
        created_at=f.created_at,
        has_sync_source=has_sync_source,
        sync_source_kind=sync_source_kind,
        sync_status=sync_status,
        owner_id=f.owner_id,
        owned=owned,
        shared=bool(f.shared),
        active=active,
    )


def _require_owner(
    db: Session, folder_id: int, user: CurrentUser
) -> Folder:
    """Authorize an owner-only mutation.

    Visible-but-not-owner gets a 403 so the SPA can render the right
    "read-only" message; folder-not-found / folder-not-visible get a 404
    so the existence of someone else's folder isn't probeable.
    """
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    if not is_folder_owner(db, folder_id, user.id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the folder owner can perform this action.",
        )
    return folder


def _resolve_managed(name: str) -> Path:
    """Validate ``name`` and return the absolute path it should occupy.

    Raises ``HTTPException`` on misconfig or unsafe input.
    """
    settings = get_settings()
    if settings.root_path is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Managed folders require VOITTA_ROOT_PATH to be configured.",
        )
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid folder name: {name!r}",
        )
    root = Path(settings.root_path).expanduser().resolve()
    target = (root / name).resolve()
    # Defence in depth against ``../`` slipping past the substring check.
    if root != target.parent:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Folder name escapes root: {name!r}",
        )
    return target


def _folder_has_active_job(db: Session, folder_id: int) -> bool:
    """True if any indexing/sync work is in flight for ``folder_id``.

    Used to gate a *physical* rename: a queued/running job captured the
    folder's OLD path (or operates on files under it), so renaming the
    directory out from under it would orphan whatever it writes. Jobs
    carry no ``folder_id`` column — the linkage lives in the JSON payload
    (``folder_id`` for folder-level jobs like ``reindex_folder`` / ``sync``;
    ``file_id`` / ``image_id`` for per-file jobs). We resolve those the
    same way the reindex endpoint does. A ``sync_status='syncing'`` source
    counts too, since a connector run may be mid-write.
    """
    src = db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_id == folder_id)
    ).scalar_one_or_none()
    if src is not None and src.sync_status == "syncing":
        return True

    inflight = db.execute(
        select(Job).where(Job.state.in_(("queued", "running")))
    ).scalars()
    for j in inflight:
        try:
            payload = json.loads(j.payload)
        except (TypeError, ValueError):
            continue
        if payload.get("folder_id") == folder_id:
            return True
        fid = payload.get("file_id")
        if not isinstance(fid, int) and "image_id" in payload:
            row = db.execute(
                select(Image.file_id).where(Image.id == int(payload["image_id"]))
            ).first()
            fid = row[0] if row is not None else None
        if isinstance(fid, int):
            f = db.get(File, fid)
            if f is not None and f.folder_id == folder_id:
                return True
    return False


def _to_file_out(f: File) -> FileOut:
    return FileOut(
        id=f.id,
        folder_id=f.folder_id,
        rel_path=f.rel_path,
        state=f.state,
        size_bytes=f.size_bytes,
        mtime_ns=f.mtime_ns,
        last_indexed_at=f.last_indexed_at,
        pending_embeds=f.pending_embeds,
        source_url=f.source_url,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FolderOut)
def create_folder(
    body: FolderIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderOut:
    abs_path = _resolve_managed(body.name)
    abs_path.mkdir(parents=True, exist_ok=True)

    existing = db.execute(
        select(Folder).where(Folder.path == str(abs_path))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Folder already registered (id={existing.id})"
        )

    folder = Folder(
        path=str(abs_path),
        display_name=body.display_name or abs_path.name or str(abs_path),
        source_type="filesystem",
        owner_id=user.id,
    )
    db.add(folder)
    db.flush()
    grant_folder(db, folder.id, user.id)
    scan = scan_folder(db, folder)
    db.commit()
    watch_folder_in_default(folder)
    out = _to_folder_out(folder, owned=True, active=True)
    events.publish("folders", {"type": "folder.added", "folder": out.model_dump()})
    # The scan may have INSERTed File rows for files that already lived
    # at this path (folder pre-existed on disk). Publish file.upserted
    # for each so the SPA's files store reflects them without waiting
    # on the next reconnect-driven listAllFiles refresh.
    for fid in scan.touched_ids:
        publish_file_upserted(fid)
    for fid in scan.vanished_ids:
        events.publish(
            "files",
            {"type": "file.deleted", "file_id": fid, "folder_id": folder.id},
        )
    return out


@router.get("/root", response_model=RootInfo)
def folder_root(
    user: CurrentUser = Depends(current_user),
) -> RootInfo:
    """Where managed folders are created, or null if not configured."""
    settings = get_settings()
    if settings.root_path is None:
        return RootInfo(root_path=None, configured=False)
    return RootInfo(root_path=str(settings.root_path), configured=True)


@router.get("/active-ids", response_model=list[int])
def active_folder_ids(
    user: CurrentUser = Depends(current_user),
) -> list[int]:
    """Folder ids that currently have at least one queued/running job.

    The SPA calls this once on bootstrap to seed its ``activeFolders``
    set; subsequent updates arrive as ``folder.active_changed`` WS
    events. We intentionally ship a flat id list (not stats) — the only
    consumer is the tree pill ("indexing" vs "indexed"), and a 19-int
    JSON array stays under 200 bytes regardless of queue depth.

    No ACL filtering: the active set is a property of the indexer
    pipeline, not of any user's view, and a folder id alone leaks no
    content. The same id would already be in the caller's
    ``GET /api/folders`` response or not — either way the visibility
    decision lives there.
    """
    from ...services.folder_active import get_active_ids

    return get_active_ids()


class UploadOut(BaseModel):
    rel_path: str
    size_bytes: int


class UploadBatchOut(BaseModel):
    files: list[UploadOut]
    count: int
    size_bytes: int


def _safe_rel_path(rel_path: str) -> Path:
    """Reject path-traversal in user-supplied rel_paths."""
    candidate = Path(rel_path.lstrip("/"))
    if candidate.is_absolute() or any(part in ("..", "") for part in candidate.parts):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid rel_path: {rel_path!r}")
    return candidate


def _safe_filename(filename: str | None) -> str:
    name = filename or "uploaded.bin"
    if "/" in name or "\\" in name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid filename: {filename!r}")
    if not name or name in (".", ".."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid filename: {filename!r}")
    return name


@router.post(
    "/{folder_id}/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadBatchOut,
)
async def upload_file(
    folder_id: int,
    request: Request,
    rel_path: str | None = None,
    rel_dir: str | None = None,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> UploadBatchOut:
    """Upload files into a folder, streaming each one to disk as it arrives.

    Bytes flow ``request.stream() → multipart parser → target_dir/.<name>.tmp``,
    then an atomic ``os.replace`` flips each finished file into place. The
    handler never buffers a whole file in memory or in ``/tmp``: peak RSS
    is one chunk (~64 KiB) regardless of upload size.

    Files commit one-by-one as their multipart part terminates, so the
    watcher can fire ``file.upserted`` and the SPA can show the file
    while later files in the same POST are still uploading.
    """
    folder = _require_owner(db, folder_id, user)
    _require_regular(folder, db.get(FolderSyncSource, folder_id), "upload into")
    folder_root = Path(folder.path).resolve()

    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "expected multipart/form-data",
        )

    rel_base = _safe_rel_path(rel_dir) if rel_dir else Path()
    uploaded = await _stream_multipart_to_folder(
        request=request,
        folder_root=folder_root,
        rel_base=rel_base,
        rel_path_override=rel_path,
    )
    return UploadBatchOut(
        files=uploaded,
        count=len(uploaded),
        size_bytes=sum(item.size_bytes for item in uploaded),
    )


async def _stream_multipart_to_folder(
    *,
    request: Request,
    folder_root: Path,
    rel_base: Path,
    rel_path_override: str | None,
) -> list[UploadOut]:
    """Drive python-multipart's streaming parser against ``request.stream()``.

    For each file part we open a hidden ``.<name>.<rand>`` sidecar in the
    target directory, write chunks straight in, and ``os.replace`` it on
    part end. Anything other than file parts (rare — the SPA passes
    ``rel_path``/``rel_dir`` as query params) is read and discarded so a
    misconfigured client can't smuggle bytes past the rel_path safety
    check.
    """
    from python_multipart.multipart import (
        MultipartParser,
        parse_options_header,
    )

    content_type = request.headers["content-type"]
    _, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if not boundary:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "multipart body missing boundary",
        )

    uploaded: list[UploadOut] = []

    # Mutable per-part state. We can't bind these as closures of the
    # callbacks (parser callbacks fire from the same thread as feed(),
    # so a plain dict works) — keeping them in one scope makes the
    # cleanup-on-error path tractable.
    state: dict = {
        "header_field": bytearray(),
        "header_value": bytearray(),
        "headers": [],
        "filename": None,
        "field_name": None,
        "out": None,           # open file handle for the active file part
        "tmp_path": None,      # sidecar path for cleanup-on-error
        "target_path": None,   # final path the sidecar will be renamed to
        "target_rel": None,    # rel-path string for the response
        "bytes_written": 0,
        "is_file_part": False,
    }

    def _close_partial_part() -> None:
        out = state.get("out")
        tmp = state.get("tmp_path")
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        if tmp is not None and Path(tmp).exists():
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
        state["out"] = None
        state["tmp_path"] = None

    def on_part_begin() -> None:
        state["headers"] = []
        state["filename"] = None
        state["field_name"] = None
        state["bytes_written"] = 0
        state["is_file_part"] = False

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["header_field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["header_value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"].append(
            (bytes(state["header_field"]), bytes(state["header_value"]))
        )
        state["header_field"] = bytearray()
        state["header_value"] = bytearray()

    def on_headers_finished() -> None:
        for name, value in state["headers"]:
            if name.lower() != b"content-disposition":
                continue
            _, opts = parse_options_header(value)
            fn = opts.get(b"filename")
            fname = opts.get(b"name")
            if fn is not None:
                state["filename"] = fn.decode("utf-8", "replace")
            if fname is not None:
                state["field_name"] = fname.decode("utf-8", "replace")
            break
        # Only ``name="file"`` parts that carry a filename are uploads.
        if state["field_name"] == "file" and state["filename"] is not None:
            target_rel = (
                _safe_rel_path(rel_path_override)
                if rel_path_override is not None
                else rel_base / _safe_filename(state["filename"])
            )
            target = (folder_root / target_rel).resolve()
            if folder_root not in target.parents and target.parent != folder_root:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "rel_path escapes folder"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            # delete=False: we manage the lifetime ourselves so we can
            # rename on success or unlink on failure.
            tmp = tempfile.NamedTemporaryFile(
                "wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            )
            state["out"] = tmp
            state["tmp_path"] = tmp.name
            state["target_path"] = target
            state["target_rel"] = target_rel.as_posix()
            state["is_file_part"] = True

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if not state["is_file_part"]:
            return  # silently drop non-file fields
        chunk = data[start:end]
        state["out"].write(chunk)
        state["bytes_written"] += len(chunk)

    def on_part_end() -> None:
        if not state["is_file_part"]:
            return
        out = state["out"]
        out.flush()
        out.close()
        os.replace(state["tmp_path"], state["target_path"])
        uploaded.append(
            UploadOut(
                rel_path=state["target_rel"],
                size_bytes=state["bytes_written"],
            )
        )
        state["out"] = None
        state["tmp_path"] = None
        # The second-file guard below uses ``is_file_part`` to detect a
        # part that's mid-flight — once we've committed this one, clear
        # the flag so trailing boundary bytes don't look like a new part.
        state["is_file_part"] = False

    def on_end() -> None:
        pass

    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
    )

    # rel_path is single-file only (matches the pre-streaming contract).
    # In streaming mode we can't know the count up front, so detect a
    # second file part as soon as it's announced and bail out before its
    # bytes start landing on disk.
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            parser.write(chunk)
            if rel_path_override is not None and len(uploaded) + (
                1 if state["is_file_part"] else 0
            ) > 1:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "rel_path can only be used when uploading one file; "
                    "use rel_dir for batches.",
                )
        parser.finalize()
    except HTTPException:
        _close_partial_part()
        raise
    except Exception as e:
        _close_partial_part()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"upload failed: {e}"
        ) from e

    # Sidecar lingering past the loop means the client cut us off
    # mid-part. Unlink so we don't leave dotfiles around.
    _close_partial_part()
    return uploaded


class MkdirIn(BaseModel):
    path: str = Field(..., description="Relative path under the folder root.")


class MkdirOut(BaseModel):
    rel_path: str


@router.post(
    "/{folder_id}/mkdir",
    status_code=status.HTTP_201_CREATED,
    response_model=MkdirOut,
)
def mkdir(
    folder_id: int,
    body: MkdirIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> MkdirOut:
    """Create an empty subdirectory inside a folder.

    Useful for organising uploads ahead of dropping files. Watcher won't see
    the empty directory (no file events), so the directory exists on disk
    but no DB rows are created.
    """
    folder = _require_owner(db, folder_id, user)
    _require_regular(folder, db.get(FolderSyncSource, folder_id), "create directories in")
    rel = _safe_rel_path(body.path)
    target = (Path(folder.path) / rel).resolve()
    folder_root = Path(folder.path).resolve()
    if folder_root not in target.parents and target != folder_root:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rel path escapes folder")
    target.mkdir(parents=True, exist_ok=True)
    return MkdirOut(rel_path=str(rel))


class ExtensionStats(BaseModel):
    files: int = 0
    indexed: int = 0
    error: int = 0
    unsupported: int = 0
    in_progress: int = 0
    pending: int = 0
    chunks: int = 0


class IndexHealth(BaseModel):
    """Cross-store sanity check: SQLite says these files are indexed, does
    Qdrant agree?

    ``status`` values:
    - ``"ok"``      — Qdrant has chunk points for this folder
    - ``"empty"``   — nothing indexed yet (no expectation either way)
    - ``"out_of_sync"`` — SQLite says indexed, Qdrant has 0 points; needs
                        Reindex to repopulate the vector store
    """

    status: str
    qdrant_chunk_points: int


class FolderStats(BaseModel):
    folder_id: int
    files_total: int
    files_indexed: int
    files_error: int
    files_unsupported: int
    # In-progress: chunks / images already committed but the file hasn't
    # finished embedding (state in ('extracted', 'embedding')). Pending: not
    # started yet (state == 'pending'). Distinguishing the two stops the
    # sidebar from reading "Pending: 329, Chunks: 1943" — which made it
    # look like nothing had happened despite half the work being done.
    files_in_progress: int
    files_pending: int
    # Cloud placeholders parked by the extract worker (subset of unsupported);
    # default keeps old serialized payloads valid.
    files_cloud_only: int = 0
    # Echo of the ?dir= scope this snapshot was computed for (None = folder).
    dir: str | None = None
    chunks_total: int
    images_total: int
    images_unique: int  # distinct image SHAs (Qdrant point count after dedup)
    bytes_total: int
    by_extension: dict[str, ExtensionStats]
    index_health: IndexHealth


@router.get("/{folder_id}/stats", response_model=FolderStats)
def folder_stats(
    folder_id: int,
    dir: str | None = None,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderStats:
    """Per-folder snapshot consumed by the SPA's Details panel.

    The same payload shape is also published over the ``folders`` WS
    topic as ``folder.stats_changed`` whenever the indexer commits any
    artifact under a folder. SPAs use this REST endpoint for first-load
    only; subsequent updates flow over the WS so chunks / images counts
    stay in lockstep with the live file states.

    ``?dir=`` scopes every count to that subdirectory (the WS push is
    always folder-level; the SPA refetches scoped stats when a subtree
    is selected).
    """
    from ...services.folder_stats import compute_folder_stats

    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    rel_prefix = str(_safe_rel_path(dir)) if dir else None
    return FolderStats(**compute_folder_stats(db, folder, rel_prefix=rel_prefix))


class GrantBody(BaseModel):
    user_id: int


def _grantee_account_ids(db: Session, user_id: int) -> list[int]:
    """Expand one account id to ALL accounts of that email.

    Sharing is person-directed: "share with ivan@…" should reach Ivan no
    matter which company account he's switched to. Snapshot semantics —
    accounts created by later logins don't inherit past grants.
    """
    from ...db.models import User as _User
    from ...services.acl import accounts_for_email

    row = db.get(_User, user_id)
    if row is None:
        return [user_id]
    return [a.id for a in accounts_for_email(db, row.email)]


@router.post("/{folder_id}/grant", status_code=status.HTTP_204_NO_CONTENT)
def grant(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    _require_owner(db, folder_id, user)
    for uid in _grantee_account_ids(db, body.user_id):
        grant_folder(db, folder_id, uid)
    db.commit()
    # Visibility changed for the grantee — make live WS connections recompute.
    events.bump_acl_version()


@router.post("/{folder_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    _require_owner(db, folder_id, user)
    for uid in _grantee_account_ids(db, body.user_id):
        revoke_folder(db, folder_id, uid)
    db.commit()
    # Visibility changed for the revokee — make live WS connections recompute.
    events.bump_acl_version()


class ShareIn(BaseModel):
    shared: bool


class ActiveIn(BaseModel):
    active: bool


@router.patch("/{folder_id}/share", response_model=FolderOut)
def set_share(
    folder_id: int,
    body: ShareIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderOut:
    """Owner-only toggle. Sharing is COMMUNITY-scoped: the folder becomes
    visible (read-only) to accounts in the owner account's community — the
    same Clerk company for a company account, or the native userbase for a
    natively-allowed Personal account. Personal accounts of Clerk-only
    users have no community and cannot share (grants still work).
    """
    from ...services.acl import account_community

    folder = _require_owner(db, folder_id, user)
    if body.shared and account_community(db, user.id) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This account has no sharing community. Switch to a company "
            "account to share with that company.",
        )
    folder.shared = bool(body.shared)
    db.commit()
    db.refresh(folder)
    sync_src = db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_id == folder_id)
    ).scalar_one_or_none()
    out = _to_folder_out(
        folder,
        has_sync_source=sync_src is not None,
        sync_source_kind=_sync_source_kind(sync_src),
        sync_status=(sync_src.sync_status if sync_src else "idle"),
        owned=True,
        active=folder_active_for_user(db, folder_id, user.id),
    )
    # A shared-flag flip changes visibility for *every* user — recompute all
    # connections' visible sets, then push the new state.
    events.bump_acl_version()
    # Push so other connected SPAs see the new sharing state without polling.
    events.publish("folders", {"type": "folder.upserted", "folder": out.model_dump()})
    return out


@router.patch("/{folder_id}/active", response_model=FolderOut)
def set_active_endpoint(
    folder_id: int,
    body: ActiveIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderOut:
    """Per-user toggle: when ``active=false`` this folder is excluded from
    the user's MCP search calls. Visible to anyone who can see the folder
    (including read-only viewers of a shared folder).
    """
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    set_folder_active(db, folder_id, user.id, bool(body.active))
    db.commit()
    sync_src = db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_id == folder_id)
    ).scalar_one_or_none()
    return _to_folder_out(
        folder,
        has_sync_source=sync_src is not None,
        sync_source_kind=_sync_source_kind(sync_src),
        sync_status=(sync_src.sync_status if sync_src else "idle"),
        owned=is_folder_owner(db, folder_id, user.id),
        active=bool(body.active),
    )


class RenameIn(BaseModel):
    """Rename a managed folder.

    ``display_name`` updates the sidebar label only — cosmetic, zero
    cascade. ``name`` additionally renames the physical directory under
    ``$VOITTA_ROOT_PATH`` and rewrites ``folders.path``. Everything heavy
    is rename-invariant: Qdrant points are keyed by ``folder_id`` + the
    file's *relative* path, CAS entries by content hash, and ``files``
    rows by ``(folder_id, rel_path)`` — none embed the top-level name, so
    a physical rename needs no re-index or vector migration. Pass either
    field or both; at least one is required.
    """

    display_name: str | None = None
    name: str | None = None


@router.patch("/{folder_id}/rename", response_model=FolderOut)
def rename_folder(
    folder_id: int,
    body: RenameIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderOut:
    """Owner-only. Update the display label and/or rename the dir on disk.

    The physical-rename path mirrors ``delete_folder``'s load-bearing
    order: validate + collision-check, refuse while jobs are in flight,
    unwatch BEFORE the move (so the watcher doesn't fire delete+create
    churn for every file as the tree moves), ``os.rename``, rewrite
    ``folders.path``, then re-anchor the watcher to the new path.
    """
    folder = _require_owner(db, folder_id, user)

    new_display = body.display_name.strip() if body.display_name is not None else None
    new_name = body.name.strip() if body.name is not None else None
    if not new_display and not new_name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Nothing to rename: provide display_name and/or name.",
        )

    current_name = Path(folder.path).name
    physically_renamed = False

    if new_name and new_name != current_name:
        new_path = _resolve_managed(new_name)  # validates + abs path under root

        clash = db.execute(
            select(Folder).where(Folder.path == str(new_path))
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A folder named {new_name!r} is already registered (id={clash.id}).",
            )
        if new_path.exists():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A directory named {new_name!r} already exists under the root.",
            )

        # Defensive: only rename folders that genuinely live under the root
        # (same guard delete_folder uses before touching disk). A hand-edited
        # DB row pointing elsewhere must not be moved.
        settings = get_settings()
        old_path = Path(folder.path)
        if settings.root_path is not None:
            resolved_root = Path(settings.root_path).expanduser().resolve()
            if not old_path.resolve().is_relative_to(resolved_root):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Folder lives outside VOITTA_ROOT_PATH; refusing to rename on disk.",
                )

        # A running/queued job captured the OLD path — renaming now would
        # orphan its output. Make the user wait for a quiescent folder.
        if _folder_has_active_job(db, folder_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Folder has indexing or sync jobs in progress; wait for them "
                "to finish before renaming the directory.",
            )

        unwatch_folder_in_default(folder_id)
        try:
            os.rename(old_path, new_path)
        except OSError as e:
            # Re-attach the watcher to the still-valid old path and surface.
            watch_folder_in_default(folder)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, f"Rename failed: {e}"
            )
        folder.path = str(new_path)
        physically_renamed = True

    if new_display:
        folder.display_name = new_display
    elif physically_renamed and folder.display_name in ("", current_name):
        # Label was just the old dir name (the create-time default) — keep
        # it tracking the physical name so it doesn't read stale.
        folder.display_name = new_name

    db.commit()
    db.refresh(folder)

    # Re-anchor the watcher AFTER commit so it picks up the new folder.path.
    if physically_renamed:
        watch_folder_in_default(folder)

    sync_src = db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_id == folder_id)
    ).scalar_one_or_none()
    out = _to_folder_out(
        folder,
        has_sync_source=sync_src is not None,
        sync_source_kind=_sync_source_kind(sync_src),
        sync_status=(sync_src.sync_status if sync_src else "idle"),
        owned=True,
        active=folder_active_for_user(db, folder_id, user.id),
    )
    events.publish("folders", {"type": "folder.upserted", "folder": out.model_dump()})
    return out


@router.get("", response_model=list[FolderOut])
def list_folders(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FolderOut]:
    rows = db.execute(select(Folder).order_by(Folder.id)).scalars().all()
    sync_rows = db.execute(select(FolderSyncSource)).scalars().all()
    sync_by_folder: dict[int, FolderSyncSource] = {
        s.folder_id: s for s in sync_rows
    }
    if get_settings().single_user:
        return [
            _to_folder_out(
                f,
                has_sync_source=f.id in sync_by_folder,
                sync_source_kind=_sync_source_kind(sync_by_folder.get(f.id)),
                sync_status=(
                    sync_by_folder[f.id].sync_status
                    if f.id in sync_by_folder
                    else "idle"
                ),
                owned=True,
                active=folder_active_for_user(db, f.id, user.id),
            )
            for f in rows
        ]
    visible = set(visible_folder_ids(db, user.id))
    return [
        _to_folder_out(
            f,
            has_sync_source=f.id in sync_by_folder,
            sync_source_kind=_sync_source_kind(sync_by_folder.get(f.id)),
            sync_status=(
                sync_by_folder[f.id].sync_status
                if f.id in sync_by_folder
                else "idle"
            ),
            owned=is_folder_owner(db, f.id, user.id),
            active=folder_active_for_user(db, f.id, user.id),
        )
        for f in rows
        if f.id in visible
    ]


class ReindexIn(BaseModel):
    rel_dir: str | None = Field(
        default=None,
        description=(
            "Optional path prefix relative to the folder root. None or empty "
            "string reindexes the entire folder; otherwise reindex applies "
            "recursively to every file under that subdirectory."
        ),
    )


class ReindexOut(BaseModel):
    folder_id: int
    rel_dir: str
    scheduled: int
    job_id: int


@router.post("/{folder_id}/reindex", response_model=ReindexOut)
def reindex_folder(
    folder_id: int,
    body: ReindexIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> ReindexOut:
    """Hard-reindex every file under a folder (or a subdirectory of it).

    Returns immediately after enqueuing a single ``reindex_folder`` job at
    high priority. The worker picks it up after its current job finishes
    (one file, max), then wipes every matched file's chunks / images /
    CAS refs / Qdrant points, resets the file rows to
    ``state='pending', file_cas_id=NULL``, and enqueues fresh extracts.

    Doing the wipe in the worker (instead of in this request thread) means
    we don't fight ``_EXTRACT_LOCK`` against an in-flight extract — by the
    time the handler runs, *we* are the extract worker, so wipes are
    uncontended. Pre-redesign this endpoint blocked under the lock for
    the duration of the running extract, with the browser timing out and
    the user seeing nothing happen.
    """
    _require_owner(db, folder_id, user)

    rel_dir = (body.rel_dir or "").strip().strip("/")
    # Block path traversal — relative paths only, no ``..`` segments. Empty
    # ``rel_dir`` is allowed and means "match the whole folder".
    if rel_dir and any(p in ("..", "") for p in rel_dir.split("/")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Invalid rel_dir: {body.rel_dir!r}"
        )

    q = select(File.id).where(File.folder_id == folder_id, File.state != "deleted")
    if rel_dir:
        like_prefix = (
            rel_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "/%"
        )
        q = q.where(File.rel_path.like(like_prefix, escape="\\"))

    file_ids = [fid for (fid,) in db.execute(q).all()]

    # Pre-emptive state flip: every targeted file goes to ``pending``
    # immediately so the SPA's per-file state pill stops saying ``indexed``
    # the moment the click lands. Without this, files stay visually
    # ``indexed`` until the worker reaches phase 3 of ``_run_reindex_sync``
    # — which can be minutes when ``_EXTRACT_LOCK`` is held by an in-flight
    # PDF — and the user has no per-file signal that anything changed.
    #
    # ``embed_round`` is bumped here so any in-flight embed completion
    # racing against the wipe takes the stale-round path in
    # ``_decrement_pending_embeds`` and doesn't accidentally heal the file
    # back to ``indexed``. ``file_cas_id`` is left untouched on purpose:
    # phase 2's CAS decref reads it back to release the file blob, and
    # nulling it here would leak a CAS ref. Phase 3 nulls it itself.
    if file_ids:
        from sqlalchemy import bindparam, update

        db.execute(
            update(File)
            .where(File.id.in_(bindparam("ids", expanding=True)))
            .values(
                state="pending",
                error=None,
                pending_embeds=0,
                embed_round=File.embed_round + 1,
            ),
            {"ids": file_ids},
        )
        db.flush()

    job_id = job_queue.enqueue(
        db,
        "reindex_folder",
        {"folder_id": folder_id, "rel_dir": rel_dir, "file_ids": file_ids},
        dedup_key=f"reindex:{folder_id}:{rel_dir}",
        priority=100,  # ahead of routine extracts so wipe runs ASAP
    )

    # Surface the queued state to the SPA the moment the click lands —
    # the worker will only emit ``phase='cancelling'`` once it actually
    # picks the job up, which can be minutes away if a long PDF is mid-
    # extract under _EXTRACT_LOCK. ``behind`` carries the rel_path of
    # the file we're waiting on (if any) so the pill can render
    # "Queued behind big.pdf" instead of just spinning.
    behind_rel: str | None = None
    running_q = db.execute(
        select(Job).where(
            Job.state == "running",
            Job.kind.in_(("extract", "embed_text", "embed_image")),
        )
    ).scalars()
    for j in running_q:
        try:
            payload = json.loads(j.payload)
        except (TypeError, ValueError):
            continue
        running_file_id = payload.get("file_id")
        if not isinstance(running_file_id, int) and "image_id" in payload:
            row = db.execute(
                select(Image.file_id).where(Image.id == int(payload["image_id"]))
            ).first()
            running_file_id = row[0] if row is not None else None
        if isinstance(running_file_id, int):
            f = db.get(File, running_file_id)
            if f is not None:
                behind_rel = f.rel_path
                break
    db.commit()

    events.publish(
        "folders",
        {
            "type": "folder.reindex_progress",
            "folder_id": folder_id,
            "phase": "queued",
            "done": 0,
            "total": len(file_ids),
            "detail": {"behind": behind_rel} if behind_rel else None,
        },
    )

    # Emit a ``file.upserted`` per targeted row so the SPA's per-file
    # state pill flips from ``indexed`` to ``pending`` without waiting
    # on a poll. The bulk UPDATE above is already committed; we reuse
    # the request session to fetch each row for its event payload.
    for fid in file_ids:
        row = db.get(File, fid)
        if row is not None:
            events.publish(
                "files",
                {"type": "file.upserted", "file": file_event_payload(row)},
            )

    return ReindexOut(
        folder_id=folder_id,
        rel_dir=rel_dir,
        scheduled=len(file_ids),
        job_id=job_id,
    )


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    """Unregister a managed folder and delete its content from disk.

    Order of operations is load-bearing: unwatch FIRST so the disk wipe
    doesn't fan a thousand ``file.deleted`` events through the watcher,
    then rmtree, then DB delete. The DB delete cascades file/chunk/image
    rows; CAS refcounts and Qdrant points belonging to the folder go
    stale but the GC sweeper / search-time ACL cover that. (Reindex
    folder uses the same shape.)

    Disk deletion is gated on the folder living under
    ``VOITTA_ROOT_PATH`` — a defensive check that should always hold
    for managed folders since external-path registration was removed,
    but if someone hand-edits the DB to point a folder at ``/`` we
    refuse to rm there. Any rmtree failure (broken symlink, perms) is
    logged but does NOT block the unregister, so the user isn't stuck
    with a dead row whose path they can't fix from the UI.
    """
    folder = _require_owner(db, folder_id, user)
    folder_path_str = folder.path

    # Stop the watcher before we touch the directory so we don't
    # broadcast a ``file.deleted`` per file as rmtree walks them.
    unwatch_folder_in_default(folder_id)

    settings = get_settings()
    root = settings.root_path
    folder_path = Path(folder_path_str)
    # Cloud-local folders are indexed IN PLACE on the user's read-only Drive
    # mount — their path is NOT app-managed storage. Never wipe it from disk:
    # a delete inside ~/Library/CloudStorage would propagate UP to Google Drive
    # via File Provider and destroy the user's cloud data. This explicit guard
    # is path-independent (does not rely on the mount happening to fall outside
    # VOITTA_ROOT_PATH), removing only the DB rows.
    if folder.source_type == "google_drive_local":
        logger.info(
            "delete_folder %d is cloud-local (read-only Drive mount) — "
            "removing index rows only, leaving Drive content untouched", folder_id
        )
    elif root is not None and folder_path.exists():
        try:
            resolved_root = root.resolve()
            resolved_folder = folder_path.resolve()
            # ``Path.is_relative_to`` is 3.9+; safe for our 3.11+ floor.
            if resolved_folder == resolved_root:
                logger.warning(
                    "delete_folder %d refusing to wipe root: %s",
                    folder_id,
                    resolved_folder,
                )
            elif not resolved_folder.is_relative_to(resolved_root):
                logger.warning(
                    "delete_folder %d path %s is outside %s — skipping disk wipe",
                    folder_id,
                    resolved_folder,
                    resolved_root,
                )
            else:
                shutil.rmtree(resolved_folder, ignore_errors=False)
                logger.info("delete_folder %d wiped %s", folder_id, resolved_folder)
        except OSError as e:
            logger.warning(
                "delete_folder %d rmtree failed for %s: %s",
                folder_id,
                folder_path_str,
                e,
            )

    db.delete(folder)
    db.commit()
    events.publish("folders", {"type": "folder.removed", "folder_id": folder_id})


def _require_regular(folder: Folder, source: FolderSyncSource | None, action: str) -> None:
    """Raise 409 if the folder has a sync source (files managed externally)."""
    if source is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot {action} a synced folder — its contents are managed by "
            "the configured sync source (Git, Google Drive, …).",
        )


@router.delete("/{folder_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    folder_id: int,
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    """Delete a single file (and its .voitta.meta sidecar if present) from a regular folder."""
    folder = _require_owner(db, folder_id, user)
    source = db.get(FolderSyncSource, folder_id)
    _require_regular(folder, source, "delete files from")

    file = db.get(File, file_id)
    if file is None or file.folder_id != folder_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")

    # Delete from disk (best-effort; DB cleanup happens regardless).
    folder_path = Path(folder.path)
    abs_path = folder_path / file.rel_path
    with contextlib.suppress(OSError):
        abs_path.unlink(missing_ok=True)
    # Also remove any .voitta.meta sidecar.
    with contextlib.suppress(OSError):
        sidecar = abs_path.parent / (abs_path.name + ".voitta.meta")
        sidecar.unlink(missing_ok=True)

    # Enqueue the wipe + row deletion via the normal worker path.
    job_queue.enqueue(db, "delete_file", {"file_id": file_id})


@router.delete("/{folder_id}/dirs", status_code=status.HTTP_204_NO_CONTENT)
def delete_subdir(
    folder_id: int,
    rel: str,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    """Delete a subdirectory and all its contents from a regular folder."""
    folder = _require_owner(db, folder_id, user)
    source = db.get(FolderSyncSource, folder_id)
    _require_regular(folder, source, "delete directories from")

    if not rel or rel in (".", "/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete the root directory.")

    # Path-safety: must resolve inside the folder root.
    folder_path = Path(folder.path).resolve()
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path traversal not allowed.")
    target = folder_path.joinpath(*parts).resolve()
    try:
        target.relative_to(folder_path)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path escapes folder root.")

    if target == folder_path:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete the root directory.")

    # Remove from disk.
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    # Mark all File rows under this subdir as deleted and enqueue wipes.
    prefix = rel.rstrip("/") + "/"
    files = db.execute(
        select(File).where(
            File.folder_id == folder_id,
            File.rel_path.like(f"{prefix}%"),
        )
    ).scalars().all()
    for f in files:
        job_queue.enqueue(db, "delete_file", {"file_id": f.id})

    # Also handle files directly in the dir (no deeper nesting).
    exact = db.execute(
        select(File).where(
            File.folder_id == folder_id,
            File.rel_path == rel.rstrip("/"),
        )
    ).scalars().first()
    if exact:
        job_queue.enqueue(db, "delete_file", {"file_id": exact.id})


@router.get("/{folder_id}/dirs")
def list_folder_dirs(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[str]:
    """Return all subdirectory rel-paths present on disk for a regular folder.

    Used by the frontend to seed empty dirs into the tree so they appear
    even before any files are indexed into them.
    """
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    # Cloud-local (Google Drive) folders: ``folder.path`` is the whole account
    # MOUNT, so walking it would return EVERY directory in the Drive as an empty
    # "ghost dir" — flooding the tree with folders the user never selected (and
    # an expensive full-Drive walk on every boot). Ghost dirs only make sense
    # for upload folders (mkdir → upload into). For a read-only Drive mirror we
    # seed nothing: the tree is built purely from indexed file paths, so only
    # folders that actually contain content appear.
    if folder.source_type == "google_drive_local":
        return []
    root = Path(folder.path)
    if not root.is_dir():
        return []
    result: list[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        # Prune hidden dirs in-place so os.walk skips their subtrees too.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for d in dirnames:
            full = Path(dirpath) / d
            try:
                rel = full.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            result.append(rel)
    return result


@router.get("/{folder_id}/files", response_model=list[FileOut])
def list_folder_files(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FileOut]:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    rows = (
        db.execute(
            select(File)
            .where(
                File.folder_id == folder_id,
                ~File.rel_path.like("%.voitta.meta"),
            )
            .order_by(File.rel_path)
        )
        .scalars()
        .all()
    )
    return [_to_file_out(f) for f in rows]
