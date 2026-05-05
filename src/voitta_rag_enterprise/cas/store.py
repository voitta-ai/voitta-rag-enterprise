"""Content-addressable storage for files and images.

Two namespaces:

- ``cas/files/<file_sha>/`` — directory holding ``text.md``, ``manifest.json``, ...
- ``cas/images/<image_sha>.bin`` — flat file with raw image bytes.

Reference counting lives in SQLite (``cas_refs``). Blobs are deleted by the
GC sweeper in ``cas.gc`` once their refcount has been zero for a quiet period.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import CasRef

KIND_FILE = "file"
KIND_IMAGE = "image"


def _root() -> Path:
    return get_settings().resolved_cas_dir()


def files_dir() -> Path:
    return _root() / "files"


def images_dir() -> Path:
    return _root() / "images"


def file_dir(file_sha: str) -> Path:
    return files_dir() / file_sha


def image_path(image_sha: str) -> Path:
    return images_dir() / f"{image_sha}.bin"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_file_blob(file_sha: str, name: str, data: bytes | str) -> Path:
    """Write a named blob inside ``cas/files/<file_sha>/``. Idempotent."""
    target = file_dir(file_sha)
    target.mkdir(parents=True, exist_ok=True)
    p = target / name
    if isinstance(data, str):
        p.write_text(data)
    else:
        p.write_bytes(data)
    return p


def read_file_blob(file_sha: str, name: str) -> bytes:
    return (file_dir(file_sha) / name).read_bytes()


def write_image_blob(data: bytes) -> str:
    """Write image bytes; return the SHA. No-op if already present."""
    sha = hash_bytes(data)
    images_dir().mkdir(parents=True, exist_ok=True)
    p = image_path(sha)
    if not p.exists():
        p.write_bytes(data)
    return sha


def read_image_blob(image_sha: str) -> bytes:
    return image_path(image_sha).read_bytes()


def incref(session: Session, kind: str, sha: str) -> int:
    """Race-free upsert that bumps refcount and clears any decref timestamp."""
    stmt = (
        sqlite_insert(CasRef)
        .values(cas_id=sha, kind=kind, refcount=1, last_decref_at=None)
        .on_conflict_do_update(
            index_elements=["cas_id", "kind"],
            set_={
                "refcount": CasRef.__table__.c.refcount + 1,
                "last_decref_at": None,
            },
        )
    )
    session.execute(stmt)
    session.expire_all()
    ref = session.execute(
        select(CasRef).where(CasRef.cas_id == sha, CasRef.kind == kind)
    ).scalar_one()
    return ref.refcount


def decref(session: Session, kind: str, sha: str) -> int:
    ref = session.execute(
        select(CasRef).where(CasRef.cas_id == sha, CasRef.kind == kind)
    ).scalar_one_or_none()
    if ref is None:
        return 0
    ref.refcount = max(0, ref.refcount - 1)
    if ref.refcount == 0:
        ref.last_decref_at = int(time.time())
    return ref.refcount


def remove_blob(kind: str, sha: str) -> bool:
    """Delete the on-disk blob. Caller manages refcount semantics."""
    if kind == KIND_FILE:
        d = file_dir(sha)
        if d.exists():
            shutil.rmtree(d)
            return True
    elif kind == KIND_IMAGE:
        p = image_path(sha)
        if p.exists():
            p.unlink()
            return True
    return False
