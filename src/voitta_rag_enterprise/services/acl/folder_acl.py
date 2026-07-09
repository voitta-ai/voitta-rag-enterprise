"""Folder/file authorization — visibility, ownership, grants, opt-outs.

Everything here keys on the ACCOUNT id (``users.id``), never the email.
Community-shared visibility delegates to :mod:`.community` — the single
org-scoping seam.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.models import File, FolderAcl, FolderUserSettings, User
from ...db.models import Folder as _Folder
from .community import _owner_community, account_community


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
    """Folders the account can see.

    Union of three sources:

    - **Owned** (``folders.owner_id == user_id``) — folders this account
      registered. Always visible to the owner.
    - **Granted** (``folder_acl(folder_id, user_id)``) — folders explicitly
      shared with this account via the grant API.
    - **Community-shared** (``folders.shared = 1``) — visible only when the
      viewer account and the folder owner's account are in the SAME
      community (same Clerk company, or both native). Sharing is
      company-centric, never global.
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
    shared: set[int] = set()
    community = account_community(session, user_id)
    if community is not None:
        for f in session.execute(
            select(_Folder).where(_Folder.shared.is_(True))
        ).scalars():
            if _owner_community(session, f.owner_id) == community:
                shared.add(f.id)
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
    if folder.owner_id == user_id:
        return True
    if folder.shared:
        # Community-scoped, mirroring visible_folder_ids: shared reaches
        # only accounts in the owner's community.
        viewer = account_community(session, user_id)
        if viewer is not None and viewer == _owner_community(session, folder.owner_id):
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
