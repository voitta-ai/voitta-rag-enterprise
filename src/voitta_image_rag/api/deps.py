"""FastAPI dependency callables."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from ..db.database import get_session_factory
from ..services.acl import CurrentUser, get_or_create_user, resolve_user_email


def db_session() -> Iterator[Session]:
    factory = get_session_factory()
    s = factory()
    try:
        yield s
    finally:
        s.close()


def current_user(
    db: Session = Depends(db_session),
    x_forwarded_email: str | None = Header(default=None, alias="X-Forwarded-Email"),
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
) -> CurrentUser:
    email = resolve_user_email(x_forwarded_email, x_user_name)
    user = get_or_create_user(db, email)
    db.commit()
    return CurrentUser(id=user.id, email=user.email)
