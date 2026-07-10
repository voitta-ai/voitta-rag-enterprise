"""Groups (organizational; no folder-ACL effect)."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....db.models import User
from ....services.acl import CurrentUser
from ...deps import admin_user, db_session
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Groups (organizational; no folder-ACL effect)
# ---------------------------------------------------------------------------


class GroupOut(BaseModel):
    id: int
    name: str
    description: str | None
    member_count: int


class _GroupCreateIn(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None


class _GroupPatchIn(BaseModel):
    name: str | None = None
    description: str | None = None


class _MemberIn(BaseModel):
    user_id: int


@router.get("/groups", response_model=list[GroupOut])
def list_groups(
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(admin_user),
) -> list[GroupOut]:
    from ....services import groups as groups_svc

    return [GroupOut(**g) for g in groups_svc.list_groups_with_counts(db)]


@router.post("/groups", response_model=GroupOut)
def create_group(
    body: _GroupCreateIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> GroupOut:
    from ....db.models import Group

    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
    existing = db.execute(select(Group).where(Group.name == name)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Group {name!r} already exists")
    grp = Group(name=name, description=(body.description or "").strip() or None)
    db.add(grp)
    db.commit()
    db.refresh(grp)
    logger.info("admin: %s created group %s", me.email, name)
    out = GroupOut(id=grp.id, name=grp.name, description=grp.description, member_count=0)
    publish_admin_state()
    return out


@router.patch("/groups/{group_id}", response_model=GroupOut)
def update_group(
    group_id: int,
    body: _GroupPatchIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> GroupOut:
    from ....db.models import Group
    from ....services import groups as groups_svc

    grp = db.get(Group, group_id)
    if grp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name can't be empty")
        clash = db.execute(
            select(Group).where(Group.name == new_name, Group.id != group_id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Group {new_name!r} already exists")
        grp.name = new_name
    if body.description is not None:
        grp.description = body.description.strip() or None
    db.commit()
    logger.info("admin: %s updated group id=%d", me.email, group_id)
    count = len(groups_svc.group_member_ids(db, group_id))
    out = GroupOut(id=grp.id, name=grp.name, description=grp.description, member_count=count)
    publish_admin_state()
    return out


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    from ....db.models import Group

    grp = db.get(Group, group_id)
    if grp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    name = grp.name
    db.delete(grp)  # memberships cascade
    db.commit()
    logger.info("admin: %s deleted group %s", me.email, name)
    publish_admin_state()


@router.post("/groups/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
def add_group_member(
    group_id: int,
    body: _MemberIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    from ....db.models import Group
    from ....services import groups as groups_svc

    if db.get(Group, group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    if db.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    groups_svc.add_member(db, group_id, body.user_id)
    db.commit()
    publish_admin_state()


@router.delete(
    "/groups/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_group_member(
    group_id: int,
    user_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> None:
    from ....services import groups as groups_svc

    groups_svc.remove_member(db, group_id, user_id)
    db.commit()
    publish_admin_state()
