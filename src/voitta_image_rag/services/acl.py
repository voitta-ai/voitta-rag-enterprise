"""ACL: user resolution + folder/file authorisation.

Modes (see ARCHITECTURE.md §9):

- ``VOITTA_SINGLE_USER=true`` → every request maps to ``root@localhost``.
- ``VOITTA_DEV_USER=<email>`` → every request maps to that email.
- multi-user (default) → email comes from ``X-Forwarded-Email`` (proxy) or
  ``X-User-Name`` (MCP). Missing → 401.

Authorisation model (v1):

- ``folder_acl`` is canonical: a user can see a folder iff there's a
  ``(folder_id, user_id)`` row.
- ``file_acl`` exists for future per-file overrides; v1 leaves it empty.
- A user can see a *file* iff they can see its folder.
- Indexers stamp ``allowed_users`` on every Qdrant chunk point at index time;
  changes to ACLs after indexing require re-indexing the file (deferred).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import File, FolderAcl, User
from ..db.models import Folder as _Folder

logger = logging.getLogger(__name__)

ROOT_EMAIL = "root@localhost"


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str


def resolve_user_email(
    x_forwarded_email: str | None,
    x_user_name: str | None,
) -> str:
    s = get_settings()
    if s.single_user:
        return ROOT_EMAIL
    if s.dev_user:
        return s.dev_user
    email = x_forwarded_email or x_user_name
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated user (set VOITTA_DEV_USER for local dev).",
        )
    return email


def get_or_create_user(session: Session, email: str) -> User:
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
        session.flush()
    return user


def grant_folder(session: Session, folder_id: int, user_id: int) -> None:
    """Idempotent folder-level grant."""
    existing = session.execute(
        select(FolderAcl).where(
            FolderAcl.folder_id == folder_id, FolderAcl.user_id == user_id
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(FolderAcl(folder_id=folder_id, user_id=user_id))


def revoke_folder(session: Session, folder_id: int, user_id: int) -> None:
    row = session.execute(
        select(FolderAcl).where(
            FolderAcl.folder_id == folder_id, FolderAcl.user_id == user_id
        )
    ).scalar_one_or_none()
    if row is not None:
        session.delete(row)


def folder_user_ids(session: Session, folder_id: int) -> list[int]:
    return [
        row.user_id
        for row in session.execute(
            select(FolderAcl).where(FolderAcl.folder_id == folder_id)
        ).scalars()
    ]


def visible_folder_ids(session: Session, user_id: int) -> list[int]:
    return [
        row.folder_id
        for row in session.execute(
            select(FolderAcl).where(FolderAcl.user_id == user_id)
        ).scalars()
    ]


def user_can_see_folder(session: Session, folder_id: int, user_id: int) -> bool:
    if get_settings().single_user:
        return session.get(_Folder, folder_id) is not None
    return (
        session.execute(
            select(FolderAcl).where(
                FolderAcl.folder_id == folder_id, FolderAcl.user_id == user_id
            )
        ).scalar_one_or_none()
        is not None
    )


def user_can_see_file(session: Session, file_id: int, user_id: int) -> bool:
    file = session.get(File, file_id)
    if file is None:
        return False
    if get_settings().single_user:
        return True
    return user_can_see_folder(session, file.folder_id, user_id)


def allowed_user_ids_for_file(session: Session, file_id: int) -> list[int]:
    """Resolve the ``allowed_users`` set the indexer stamps onto Qdrant chunks."""
    file = session.get(File, file_id)
    if file is None:
        return []
    return folder_user_ids(session, file.folder_id)


def seed_users_from_file(session: Session, path: Path) -> int:
    """Create ``users`` rows for every email in ``path`` (one per line). Returns count added."""
    if not path.exists():
        return 0
    added = 0
    for line in path.read_text().splitlines():
        email = line.strip()
        if not email or email.startswith("#"):
            continue
        existing = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if existing is None:
            session.add(User(email=email))
            added += 1
    if added:
        session.flush()
    return added


def public_user_ids(session: Session) -> list[int]:
    """All user ids known to the system. Used to grant a folder to everyone."""
    return [u.id for u in session.execute(select(User)).scalars()]
