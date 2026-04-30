"""Folder registration + reconciliation endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import Chunk, File, Folder, Image
from ...services import events, job_queue
from ...services.acl import (
    CurrentUser,
    grant_folder,
    revoke_folder,
    user_can_see_folder,
    visible_folder_ids,
)
from ...services.scanner import scan_folder
from ...services.watcher import unwatch_folder_in_default, watch_folder_in_default
from ..deps import current_user, db_session

router = APIRouter(prefix="/folders", tags=["folders"])


class FolderIn(BaseModel):
    """Two modes:

    - **External**: pass ``path`` (absolute host path that already exists).
      ``managed=False``; never gets a sync connector.
    - **Managed**: pass ``name`` (single path segment). Server creates
      ``$VOITTA_ROOT_PATH/<name>`` if missing. ``managed=True``; sync
      connectors can later be attached.
    """

    path: str | None = Field(default=None, description="Absolute host path (external mode)")
    name: str | None = Field(default=None, description="Folder name under VOITTA_ROOT_PATH (managed mode)")
    display_name: str | None = None


class FolderOut(BaseModel):
    id: int
    path: str
    display_name: str
    source_type: str
    enabled: bool
    managed: bool
    created_at: int


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


def _to_folder_out(f: Folder) -> FolderOut:
    return FolderOut(
        id=f.id,
        path=f.path,
        display_name=f.display_name,
        source_type=f.source_type,
        enabled=f.enabled,
        managed=f.managed,
        created_at=f.created_at,
    )


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
    if bool(body.path) == bool(body.name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide exactly one of: 'path' (external) or 'name' (managed under VOITTA_ROOT_PATH)",
        )

    if body.name is not None:
        abs_path = _resolve_managed(body.name)
        abs_path.mkdir(parents=True, exist_ok=True)
        managed = True
    else:
        abs_path = Path(body.path).expanduser().resolve()
        if not abs_path.exists() or not abs_path.is_dir():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Path does not exist or is not a directory: {abs_path}",
            )
        managed = False

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
        managed=managed,
    )
    db.add(folder)
    db.flush()
    grant_folder(db, folder.id, user.id)
    scan_folder(db, folder)
    db.commit()
    watch_folder_in_default(folder)
    out = _to_folder_out(folder)
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


def _safe_rel_path(rel_path: str) -> Path:
    """Reject path-traversal in user-supplied rel_paths."""
    candidate = Path(rel_path.lstrip("/"))
    if candidate.is_absolute() or any(part in ("..", "") for part in candidate.parts):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid rel_path: {rel_path!r}")
    return candidate


@router.post(
    "/{folder_id}/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadOut,
)
async def upload_file(
    folder_id: int,
    file: UploadFile,
    rel_path: str | None = None,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> UploadOut:
    """Upload a file into a managed folder. The watcher picks it up and indexes it.

    External (non-managed) folders are read-only via the API by design — write
    files there with your usual tooling and the watcher will catch the change.
    """
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    if not folder.managed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Uploads are only allowed on managed folders (created with {'name': ...}).",
        )

    target_rel = _safe_rel_path(rel_path or file.filename or "uploaded.bin")
    target = (Path(folder.path) / target_rel).resolve()
    folder_root = Path(folder.path).resolve()
    if folder_root not in target.parents and target.parent != folder_root:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rel_path escapes folder")

    target.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            bytes_written += len(chunk)
    return UploadOut(rel_path=str(target_rel), size_bytes=bytes_written)


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
    """Create an empty subdirectory inside a managed folder.

    Useful for organising uploads ahead of dropping files. Watcher won't see
    the empty directory (no file events), so the directory exists on disk
    but no DB rows are created.
    """
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    if not folder.managed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Subfolders are only allowed under managed folders.",
        )
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
    pending: int = 0
    chunks: int = 0


class FolderStats(BaseModel):
    folder_id: int
    files_total: int
    files_indexed: int
    files_error: int
    files_unsupported: int
    files_pending: int
    chunks_total: int
    images_total: int
    images_unique: int  # distinct image SHAs (Qdrant point count after dedup)
    bytes_total: int
    by_extension: dict[str, ExtensionStats]


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
    files_total = len(files)
    files_indexed = sum(1 for f in files if f.state == "indexed")
    files_error = sum(1 for f in files if f.state == "error")
    files_unsupported = sum(1 for f in files if f.state == "unsupported")
    files_pending = sum(
        1
        for f in files
        if f.state not in ("indexed", "error", "unsupported", "deleted")
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

    return FolderStats(
        folder_id=folder_id,
        files_total=files_total,
        files_indexed=files_indexed,
        files_error=files_error,
        files_unsupported=files_unsupported,
        files_pending=files_pending,
        chunks_total=int(chunks_total),
        images_total=int(images_total),
        images_unique=int(images_unique),
        bytes_total=bytes_total,
        by_extension=by_extension,
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
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    grant_folder(db, folder_id, body.user_id)
    db.commit()


@router.post("/{folder_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke(
    folder_id: int,
    body: GrantBody,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    revoke_folder(db, folder_id, body.user_id)
    db.commit()


@router.get("", response_model=list[FolderOut])
def list_folders(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FolderOut]:
    rows = db.execute(select(Folder).order_by(Folder.id)).scalars().all()
    if get_settings().single_user:
        return [_to_folder_out(f) for f in rows]
    visible = set(visible_folder_ids(db, user.id))
    return [_to_folder_out(f) for f in rows if f.id in visible]


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


@router.post("/{folder_id}/reindex", response_model=ReindexOut)
def reindex_folder(
    folder_id: int,
    body: ReindexIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> ReindexOut:
    """Hard-reindex every file under a folder (or a subdirectory of it).

    Bypasses the unchanged-SHA short-circuit in ``_run_extract_sync`` by
    blanking ``file_cas_id`` before enqueuing — useful after the parser is
    upgraded and the existing markdown is no longer the best representation.

    The current files are kept, but their state is reset to ``pending`` so
    the UI immediately reflects that work is in flight; ``_commit_indexing``
    bumps ``embed_round`` and resets ``pending_embeds`` when extract runs.
    """
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    rel_dir = (body.rel_dir or "").strip().strip("/")
    # Block path traversal — relative paths only, no ``..`` segments. Empty
    # ``rel_dir`` is allowed and means "match the whole folder".
    if rel_dir and any(p in ("..", "") for p in rel_dir.split("/")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Invalid rel_dir: {body.rel_dir!r}"
        )

    q = select(File).where(File.folder_id == folder_id, File.state != "deleted")
    if rel_dir:
        # SQLite LIKE is case-sensitive when the operand is BLOB-like; rel_path
        # is TEXT, so a literal prefix works. We escape ``%``/``_`` in the
        # caller-supplied dir name to keep the match exact.
        like_prefix = (
            rel_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "/%"
        )
        q = q.where(File.rel_path.like(like_prefix, escape="\\"))

    candidates = list(db.execute(q).scalars())
    scheduled = 0
    for f in candidates:
        f.file_cas_id = None  # force re-extract regardless of SHA
        f.state = "pending"
        f.error = None
        # We deliberately leave ``pending_embeds`` and ``embed_round`` alone;
        # ``_commit_indexing`` bumps the round and resets the counter when the
        # extract job runs, and any stale embeds short-circuit on round.
        job_queue.enqueue(
            db, "extract", {"file_id": f.id}, dedup_key=f"extract:{f.id}"
        )
        scheduled += 1
    db.commit()

    for f in candidates:
        events.publish(
            "files",
            {
                "type": "file.upserted",
                "file": _to_file_out(f).model_dump(),
            },
        )

    return ReindexOut(folder_id=folder_id, rel_dir=rel_dir, scheduled=scheduled)


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    folder = db.get(Folder, folder_id)
    if folder is None or not user_can_see_folder(db, folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
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
