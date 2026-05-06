"""Folder registration + reconciliation endpoints."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import Chunk, File, Folder, FolderSyncSource, Image
from ...services import events, job_queue
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


def _to_folder_out(
    f: Folder,
    *,
    has_sync_source: bool = False,
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
    scan_folder(db, folder)
    db.commit()
    watch_folder_in_default(folder)
    out = _to_folder_out(folder, owned=True, active=True)
    events.publish("folders", {"type": "folder.added", "folder": out.model_dump()})
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
    chunks_total: int
    images_total: int
    images_unique: int  # distinct image SHAs (Qdrant point count after dedup)
    bytes_total: int
    by_extension: dict[str, ExtensionStats]
    index_health: IndexHealth


@router.get("/{folder_id}/stats", response_model=FolderStats)
def folder_stats(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FolderStats:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    files = list(
        db.execute(
            select(File).where(File.folder_id == folder_id, File.state != "deleted")
        ).scalars()
    )
    _IN_PROGRESS_STATES = ("extracted", "embedding")
    files_total = len(files)
    files_indexed = sum(1 for f in files if f.state == "indexed")
    files_error = sum(1 for f in files if f.state == "error")
    files_unsupported = sum(1 for f in files if f.state == "unsupported")
    files_in_progress = sum(1 for f in files if f.state in _IN_PROGRESS_STATES)
    files_pending = sum(
        1
        for f in files
        if f.state == "pending"
    )
    bytes_total = sum(f.size_bytes or 0 for f in files)
    file_ids = [f.id for f in files]

    # Per-extension chunk counts: one COUNT-grouped-by query and a join-back
    # in Python so we don't have to pull every chunk row.
    chunks_by_file: dict[int, int] = {}
    if file_ids:
        chunks_by_file = dict(
            db.execute(
                select(Chunk.file_id, func.count(Chunk.id))
                .where(Chunk.file_id.in_(file_ids))
                .group_by(Chunk.file_id)
            ).all()
        )

    by_extension: dict[str, ExtensionStats] = {}
    for f in files:
        ext = Path(f.rel_path).suffix.lower() or "(no ext)"
        es = by_extension.setdefault(ext, ExtensionStats())
        es.files += 1
        if f.state == "indexed":
            es.indexed += 1
        elif f.state == "error":
            es.error += 1
        elif f.state == "unsupported":
            es.unsupported += 1
        elif f.state in _IN_PROGRESS_STATES:
            es.in_progress += 1
        else:
            es.pending += 1
        es.chunks += chunks_by_file.get(f.id, 0)

    chunks_total = sum(chunks_by_file.values())
    images_total = (
        db.execute(
            select(func.count(Image.id)).where(Image.file_id.in_(file_ids))
        ).scalar_one()
        if file_ids
        else 0
    )
    images_unique = (
        db.execute(
            select(func.count(func.distinct(Image.image_cas_id))).where(
                Image.file_id.in_(file_ids)
            )
        ).scalar_one()
        if file_ids
        else 0
    )

    from ...services.reconcile import folder_health

    health = folder_health(db, folder)

    return FolderStats(
        folder_id=folder_id,
        files_total=files_total,
        files_indexed=files_indexed,
        files_error=files_error,
        files_unsupported=files_unsupported,
        files_in_progress=files_in_progress,
        files_pending=files_pending,
        chunks_total=int(chunks_total),
        images_total=int(images_total),
        images_unique=int(images_unique),
        bytes_total=bytes_total,
        by_extension=by_extension,
        index_health=IndexHealth(
            status=health.status,
            qdrant_chunk_points=health.qdrant_chunk_points,
        ),
    )


class GrantBody(BaseModel):
    user_id: int


@router.post("/{folder_id}/grant", status_code=status.HTTP_204_NO_CONTENT)
def grant(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    _require_owner(db, folder_id, user)
    grant_folder(db, folder_id, body.user_id)
    db.commit()


@router.post("/{folder_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    _require_owner(db, folder_id, user)
    revoke_folder(db, folder_id, body.user_id)
    db.commit()


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
    """Owner-only toggle: when ``shared=true`` every signed-in user sees the
    folder in their listing (read-only for non-owners). Default off.
    """
    folder = _require_owner(db, folder_id, user)
    folder.shared = bool(body.shared)
    db.commit()
    db.refresh(folder)
    has_sync = (
        db.execute(
            select(FolderSyncSource.folder_id).where(
                FolderSyncSource.folder_id == folder_id
            )
        ).first()
        is not None
    )
    out = _to_folder_out(
        folder,
        has_sync_source=has_sync,
        owned=True,
        active=folder_active_for_user(db, folder_id, user.id),
    )
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
    has_sync = (
        db.execute(
            select(FolderSyncSource.folder_id).where(
                FolderSyncSource.folder_id == folder_id
            )
        ).first()
        is not None
    )
    return _to_folder_out(
        folder,
        has_sync_source=has_sync,
        owned=is_folder_owner(db, folder_id, user.id),
        active=bool(body.active),
    )


@router.get("", response_model=list[FolderOut])
def list_folders(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FolderOut]:
    rows = db.execute(select(Folder).order_by(Folder.id)).scalars().all()
    sync_ids = {
        fid for (fid,) in db.execute(select(FolderSyncSource.folder_id)).all()
    }
    if get_settings().single_user:
        return [
            _to_folder_out(
                f,
                has_sync_source=f.id in sync_ids,
                owned=True,
                active=folder_active_for_user(db, f.id, user.id),
            )
            for f in rows
        ]
    visible = set(visible_folder_ids(db, user.id))
    return [
        _to_folder_out(
            f,
            has_sync_source=f.id in sync_ids,
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
    job_id = job_queue.enqueue(
        db,
        "reindex_folder",
        {"folder_id": folder_id, "rel_dir": rel_dir, "file_ids": file_ids},
        dedup_key=f"reindex:{folder_id}:{rel_dir}",
        priority=100,  # ahead of routine extracts so wipe runs ASAP
    )
    db.commit()

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
    folder = _require_owner(db, folder_id, user)
    db.delete(folder)
    db.commit()
    unwatch_folder_in_default(folder_id)
    events.publish("folders", {"type": "folder.removed", "folder_id": folder_id})


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
            select(File).where(File.folder_id == folder_id).order_by(File.rel_path)
        )
        .scalars()
        .all()
    )
    return [_to_file_out(f) for f in rows]
