"""FastAPI dependency callables."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, Request
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
    request: Request,
    db: Session = Depends(db_session),
    x_forwarded_email: str | None = Header(default=None, alias="X-Forwarded-Email"),
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
) -> CurrentUser:
    # SessionMiddleware exposes ``request.session`` as a dict; ``user_email``
    # is set by the Google OAuth callback.
    session_email = request.session.get("user_email") if hasattr(request, "session") else None
    email = resolve_user_email(
        x_forwarded_email, x_user_name, session_email=session_email
    )
    user = get_or_create_user(db, email)
    db.commit()
    return CurrentUser(id=user.id, email=user.email)
