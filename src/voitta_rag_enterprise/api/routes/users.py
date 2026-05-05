"""User listing + ``/me`` endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import User
from ...services.acl import CurrentUser, get_or_create_user
from ..deps import current_user, db_session

router = APIRouter(prefix="/users", tags=["users"])


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    created_at: int


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str | None = None


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, email=u.email, display_name=u.display_name, created_at=u.created_at
    )


@router.get("/me", response_model=UserOut)
def me(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> UserOut:
    row = db.get(User, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User row missing")
    return _to_out(row)


@router.get("", response_model=list[UserOut])
def list_users(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[UserOut]:
    rows = db.execute(select(User).order_by(User.id)).scalars().all()
    return [_to_out(u) for u in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> UserOut:
    new = get_or_create_user(db, body.email)
    if body.display_name and new.display_name != body.display_name:
        new.display_name = body.display_name
    db.commit()
    return _to_out(new)
