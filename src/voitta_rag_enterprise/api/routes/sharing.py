"""Folder sharing configuration — the share modal's API surface.

Visibility is the UNION of independent layers (see
``services/acl/folder_acl.visible_folder_ids``):

* audience  — ``folders.shared`` (Clerk org / native-everyone; owner's
  community decides which; ``PATCH /folders/{id}/share`` toggles it)
* groups    — ``folder_group_acl`` (voitta-native groups)
* people    — ``folder_email_acl`` (lowercased emails; NOT org-restricted;
  addresses that haven't signed up yet are live "pending" shares) merged
  with legacy per-account ``folder_acl`` grants for display

Layers never mask each other: turning the audience share off leaves
group/people shares fully intact, and vice versa. Every mutation bumps the
ACL version (live WS visibility recompute) and pushes a ``folder.upserted``
so tree share-pills update everywhere without polling.

All mutations are owner-gated (``_require_owner``) and resolve through
``current_user`` — they work under admin impersonation exactly like the
sync endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import (
    Folder,
    FolderAcl,
    FolderEmailAcl,
    FolderGroupAcl,
    FolderSyncSource,
    Group,
    UserGroup,
)
from ...services import events
from ...services.acl import (
    CurrentUser,
    accounts_for_email,
    folder_active_for_user,
)
from ...services.sharing_view import GroupShareOut, SharingOut, sharing_view
from ..deps import current_user, db_session
from .folders import (
    _require_owner,
    _sync_source_kind,
    _to_folder_out,
    share_counts_for_folder,
)

router = APIRouter(prefix="/folders/{folder_id}/sharing", tags=["sharing"])
groups_router = APIRouter(prefix="/groups", tags=["sharing"])


# ---------------------------------------------------------------------------
# Request bodies (response shapes live in services/sharing_view.py)
# ---------------------------------------------------------------------------


class GroupShareIn(BaseModel):
    group_id: int


class EmailShareIn(BaseModel):
    email: str = Field(..., min_length=3)


# ---------------------------------------------------------------------------
# Change propagation
# ---------------------------------------------------------------------------


def _push_folder_update(db: Session, folder: Folder, user: CurrentUser) -> None:
    """Recompute visibility everywhere and refresh tree share-pills."""
    events.bump_acl_version()
    sync_src = db.execute(
        select(FolderSyncSource).where(FolderSyncSource.folder_id == folder.id)
    ).scalar_one_or_none()
    out = _to_folder_out(
        folder,
        has_sync_source=sync_src is not None,
        sync_source_kind=_sync_source_kind(sync_src),
        sync_status=(sync_src.sync_status if sync_src else "idle"),
        owned=True,
        active=folder_active_for_user(db, folder.id, user.id),
        share_counts=share_counts_for_folder(db, folder.id),
    )
    # ``owned``/``writable``/``active`` are per-viewer — this event goes to
    # every subscriber, and the client merges fields over its store, so
    # broadcasting the mutating owner's values would mark the folder
    # owned/writable in everyone else's tree. Send a partial payload
    # (like sync/base.publish_folder_changed) and let each viewer keep
    # their own flags.
    events.publish(
        "folders",
        {
            "type": "folder.upserted",
            "folder": out.model_dump(exclude={"owned", "writable", "active"}),
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SharingOut)
def get_sharing(
    folder_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SharingOut:
    folder = _require_owner(db, folder_id, user)
    return sharing_view(db, folder)


@router.post("/group", response_model=SharingOut)
def share_to_group(
    folder_id: int,
    body: GroupShareIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SharingOut:
    folder = _require_owner(db, folder_id, user)
    if db.get(Group, body.group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    existing = db.get(FolderGroupAcl, (folder_id, body.group_id))
    if existing is None:
        db.add(FolderGroupAcl(folder_id=folder_id, group_id=body.group_id))
        db.commit()
        _push_folder_update(db, folder, user)
    return sharing_view(db, folder)


@router.delete("/group/{group_id}", response_model=SharingOut)
def unshare_group(
    folder_id: int,
    group_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SharingOut:
    folder = _require_owner(db, folder_id, user)
    db.execute(
        sa_delete(FolderGroupAcl).where(
            FolderGroupAcl.folder_id == folder_id,
            FolderGroupAcl.group_id == group_id,
        )
    )
    db.commit()
    _push_folder_update(db, folder, user)
    return sharing_view(db, folder)


@router.post("/email", response_model=SharingOut)
def share_to_email(
    folder_id: int,
    body: EmailShareIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SharingOut:
    folder = _require_owner(db, folder_id, user)
    email = body.email.strip().lower()
    if "@" not in email or " " in email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Enter a valid email address."
        )
    existing = db.get(FolderEmailAcl, (folder_id, email))
    if existing is None:
        db.add(FolderEmailAcl(folder_id=folder_id, email=email))
        db.commit()
        _push_folder_update(db, folder, user)
    return sharing_view(db, folder)


@router.delete("/email", response_model=SharingOut)
def unshare_email(
    folder_id: int,
    email: str,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> SharingOut:
    """Remove an email share COMPLETELY: the email row AND any legacy
    per-account ``folder_acl`` grants for accounts carrying that email —
    so Remove always fully removes, with no zombie access left behind."""
    folder = _require_owner(db, folder_id, user)
    email = email.strip().lower()
    db.execute(
        sa_delete(FolderEmailAcl).where(
            FolderEmailAcl.folder_id == folder_id, FolderEmailAcl.email == email
        )
    )
    account_ids = [a.id for a in accounts_for_email(db, email)]
    if account_ids:
        db.execute(
            sa_delete(FolderAcl).where(
                FolderAcl.folder_id == folder_id,
                FolderAcl.user_id.in_(account_ids),
            )
        )
    db.commit()
    _push_folder_update(db, folder, user)
    return sharing_view(db, folder)


@groups_router.get("", response_model=list[GroupShareOut])
def list_groups(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),  # noqa: ARG001 — auth gate
) -> list[GroupShareOut]:
    """Group pick-list for the share modal (any signed-in user).

    Names + member counts only; membership editing stays in the admin
    console (``/api/admin/groups``).
    """
    rows = db.execute(
        select(Group, func.count(UserGroup.user_id))
        .outerjoin(UserGroup, UserGroup.group_id == Group.id)
        .group_by(Group.id)
        .order_by(Group.name)
    ).all()
    return [
        GroupShareOut(id=g.id, name=g.name, member_count=int(n)) for g, n in rows
    ]
