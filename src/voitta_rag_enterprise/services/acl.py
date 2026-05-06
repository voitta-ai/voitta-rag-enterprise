"""ACL: user resolution + folder/file authorisation.

Modes:

- ``VOITTA_SINGLE_USER=true`` → every request maps to ``root@localhost``.
- ``VOITTA_DEV_USER=<email>`` → every request maps to that email.
- multi-user (default) → REST identity comes from the signed session cookie
  set by Google OAuth; MCP identity comes from the verified ``Authorization:
  Bearer vk_…`` token. No header-based fallbacks.

Authorisation model (v2):

A folder has *one owner* (``folders.owner_id``) and an optional set of
explicit grants (``folder_acl``). It can also be marked ``shared=true``,
which makes it visible to every signed-in user.

Visibility (who sees the folder in their listing / can read its files):

    visible(user) = owned(user) | granted(user) | {f for f in folders if f.shared}

Mutation (delete, rename, share-toggle, sync configure, reindex, upload,
mkdir, grant, revoke): owner only.

MCP search visibility further intersects with the user's per-folder
``active`` flag (``folder_user_settings.active``); default-on means
"missing row = active". This lets a user mute folders they can see but
don't want polluting LLM search results.

Indexers still stamp ``allowed_users`` on every Qdrant chunk point at
index time, but the runtime search filter has moved to ``folder_id`` (built
from ``visible_folder_ids``) — that way shared-folder visibility doesn't
require re-indexing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import File, FolderAcl, FolderUserSettings, User
from ..db.models import Folder as _Folder

logger = logging.getLogger(__name__)

ROOT_EMAIL = "root@localhost"


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str


def resolve_user_email(session_email: str | None = None) -> str:
    """Pick the email for this request.

    Priority — first match wins:

    1. ``VOITTA_SINGLE_USER`` → ``root@localhost`` (local dev)
    2. ``VOITTA_DEV_USER`` → that email (local dev)
    3. ``session_email`` (signed cookie set by Google login)
    4. raise 401

    Self-asserted headers (``X-Forwarded-Email``, ``X-User-Name``) used to be
    accepted; they're not anymore. Web requests authenticate via the session
    cookie, MCP requests via ``Authorization: Bearer`` (handled in
    ``mcp_server.BearerAuthMiddleware``, not here).
    """
    s = get_settings()
    if s.single_user:
        return ROOT_EMAIL
    if s.dev_user:
        return s.dev_user
    if not session_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in. Sign in with Google, or set VOITTA_DEV_USER for local dev.",
        )
    return session_email


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
    """Folders the user can see.

    Union of three sources:

    - **Owned** (``folders.owner_id == user_id``) — folders this user
      registered. Always visible to the owner.
    - **Granted** (``folder_acl(folder_id, user_id)``) — folders explicitly
      shared with this user via the grant API.
    - **Shared globally** (``folders.shared = 1``) — folders the owner
      flipped to public; visible to everyone (read-only for non-owners).
    """
    owned = {
        f.id
        for f in session.execute(
            select(_Folder).where(_Folder.owner_id == user_id)
        ).scalars()
    }
    granted = {
        row.folder_id
        for row in session.execute(
            select(FolderAcl).where(FolderAcl.user_id == user_id)
        ).scalars()
    }
    shared = {
        f.id
        for f in session.execute(
            select(_Folder).where(_Folder.shared.is_(True))
        ).scalars()
    }
    return sorted(owned | granted | shared)


def mcp_visible_folder_ids(session: Session, user_id: int) -> list[int]:
    """Visible folders minus the user's MCP-search opt-outs.

    A row in ``folder_user_settings`` with ``active=0`` excludes the
    folder from this user's MCP queries; missing row = active. Used by
    the MCP server, not by the SPA.
    """
    base = visible_folder_ids(session, user_id)
    if not base:
        return []
    inactive = {
        row.folder_id
        for row in session.execute(
            select(FolderUserSettings).where(
                FolderUserSettings.user_id == user_id,
                FolderUserSettings.active.is_(False),
            )
        ).scalars()
    }
    return [fid for fid in base if fid not in inactive]


def user_can_see_folder(session: Session, folder_id: int, user_id: int) -> bool:
    if get_settings().single_user:
        return session.get(_Folder, folder_id) is not None
    folder = session.get(_Folder, folder_id)
    if folder is None:
        return False
    if folder.owner_id == user_id or folder.shared:
        return True
    return (
        session.execute(
            select(FolderAcl).where(
                FolderAcl.folder_id == folder_id, FolderAcl.user_id == user_id
            )
        ).scalar_one_or_none()
        is not None
    )


def is_folder_owner(session: Session, folder_id: int, user_id: int) -> bool:
    """True if ``user_id`` is the registered owner of ``folder_id``.

    Single-user mode always returns True so the local-dev shortcut keeps
    working without a real owner relationship.
    """
    if get_settings().single_user:
        return session.get(_Folder, folder_id) is not None
    folder = session.get(_Folder, folder_id)
    if folder is None:
        return False
    return folder.owner_id == user_id


def set_folder_active(
    session: Session, folder_id: int, user_id: int, active: bool
) -> None:
    """Upsert the user's per-folder ``active`` flag.

    Avoids leaving a row in the default state by deleting when ``active``
    flips back to True (default-on means "no row needed"). Reduces table
    size when most users default-on most folders.
    """
    row = session.execute(
        select(FolderUserSettings).where(
            FolderUserSettings.folder_id == folder_id,
            FolderUserSettings.user_id == user_id,
        )
    ).scalar_one_or_none()
    if active:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(
            FolderUserSettings(folder_id=folder_id, user_id=user_id, active=False)
        )
    else:
        row.active = False


def folder_user_id_email(session: Session, user_id: int) -> str | None:
    """Resolve user_id → email; ``None`` if the row no longer exists."""
    user = session.get(User, user_id)
    return user.email if user else None


def folder_active_for_user(session: Session, folder_id: int, user_id: int) -> bool:
    """Default-on lookup: missing row = active."""
    row = session.execute(
        select(FolderUserSettings).where(
            FolderUserSettings.folder_id == folder_id,
            FolderUserSettings.user_id == user_id,
        )
    ).scalar_one_or_none()
    return True if row is None else bool(row.active)


def active_folder_ids(session: Session, user_id: int, folder_ids: list[int]) -> set[int]:
    """Subset of ``folder_ids`` for which the user has not opted out."""
    if not folder_ids:
        return set()
    inactive = {
        row.folder_id
        for row in session.execute(
            select(FolderUserSettings).where(
                FolderUserSettings.user_id == user_id,
                FolderUserSettings.folder_id.in_(folder_ids),
                FolderUserSettings.active.is_(False),
            )
        ).scalars()
    }
    return {fid for fid in folder_ids if fid not in inactive}


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
    """Create ``users`` rows for every email in ``path`` (one per line). Returns count added.

    Used at startup to bootstrap a new install with the addresses listed
    in ``users.txt``. The runtime sign-in gate no longer consults this
    file — see ``services/admin_store.py`` for the live allowlist.
    """
    if not path.exists():
        return 0
    added = 0
    for raw in path.read_text().splitlines():
        email = raw.strip()
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
