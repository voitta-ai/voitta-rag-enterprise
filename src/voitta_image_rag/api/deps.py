"""FastAPI dependency callables."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
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
) -> CurrentUser:
    """Resolve the calling user for REST routes.

    Web identity is the signed session cookie set by the Google OAuth
    callback. ``VOITTA_SINGLE_USER`` / ``VOITTA_DEV_USER`` short-circuit
    that for local development. No header-based fallbacks; MCP routes
    authenticate separately via ``BearerAuthMiddleware``.
    """
    session_email = (
        request.session.get("user_email") if hasattr(request, "session") else None
    )
    email = resolve_user_email(session_email)
    user = get_or_create_user(db, email)
    db.commit()
    return CurrentUser(id=user.id, email=user.email)
