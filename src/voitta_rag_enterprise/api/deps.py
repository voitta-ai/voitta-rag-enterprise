"""FastAPI dependency callables."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.database import get_session_factory
from ..db.models import User
from ..services.acl import (
    ROOT_EMAIL,
    CurrentUser,
    default_account_for_email,
    offered_accounts_for_email,
    person_is_admin,
    resolve_user_email,
    stamp_person_admin,
)


def _cu(u: User) -> CurrentUser:
    return CurrentUser(
        id=u.id,
        email=u.email,
        company_id=u.company_id or "",
        company_name=u.company_name or "",
    )


def _resolve_account(
    db: Session, email: str, session: Mapping[str, Any] | None
) -> User:
    """Resolve the ACTIVE account row for an authenticated email.

    ``active_account_id`` in the session picks among the email's OFFERED
    accounts (Personal only for natively-allowed emails; company accounts
    for Clerk users). It's validated on every request — a stale, forged,
    or no-longer-offered id falls back to the default account rather than
    crossing identities or landing a Clerk-only user in their hidden
    Personal scope. Dev/single-user resolve to Personal via the fallback.
    """
    active_id = session.get("active_account_id") if session else None
    if active_id is not None:
        row = db.get(User, int(active_id))
        if row is not None and row.email == email and any(
            a.id == row.id for a in offered_accounts_for_email(db, email)
        ):
            return row
    return default_account_for_email(db, email)


def db_session() -> Iterator[Session]:
    factory = get_session_factory()
    s = factory()
    try:
        yield s
    finally:
        s.close()


def real_user(
    request: Request,
    db: Session = Depends(db_session),
) -> CurrentUser:
    """The authenticated identity, ignoring impersonation.

    Used by admin endpoints — only the *real* user's admin flag may grant
    access; an impersonated user does not inherit privileges.

    As a side-effect, every super-admin sign-in (including the
    ``VOITTA_DEV_USER`` shortcut) re-stamps ``is_admin=True``. The OAuth
    callback does the same, but for dev/single-user mode this is the
    only place the stamp can land — there is no callback in those flows.
    """
    from ..services.admin_store import is_super_admin

    sess = request.session if hasattr(request, "session") else None
    session_email = sess.get("user_email") if sess else None
    email = resolve_user_email(session_email)
    u = _resolve_account(db, email, sess)
    if is_super_admin(email) and not u.is_admin:
        stamp_person_admin(db, email, True)
    db.commit()
    return _cu(u)


def current_user(
    request: Request,
    db: Session = Depends(db_session),
) -> CurrentUser:
    """The effective identity for the request.

    Same as ``real_user`` unless the caller is an admin and has chosen
    "view as <other>" (a session-stored impersonation). Then we return
    the impersonated user so all downstream ACL/visibility code applies
    that user's permissions transparently. Admin checks always go
    through ``real_user``, never this.
    """
    me = real_user(request, db)
    sess = request.session if hasattr(request, "session") else None
    target_id = sess.get("acting_as_user_id") if sess else None
    if target_id is None:
        return me

    # Impersonation only honoured for admins (person-level: any of the
    # email's accounts carrying the flag). If the flag survives a demotion
    # we silently strip it so the user sees their own data, not a phantom.
    if not person_is_admin(db, me.email):
        if sess is not None:
            sess.pop("acting_as_user_id", None)
        return me

    target = db.get(User, int(target_id))
    if target is None:
        if sess is not None:
            sess.pop("acting_as_user_id", None)
        return me
    return _cu(target)


def resolve_ws_user(
    session: Mapping[str, Any] | None,
    db: Session,
) -> tuple[CurrentUser, bool] | None:
    """Resolve ``(effective_user, real_is_admin)`` for a WebSocket connection.

    Mirrors :func:`current_user` — including the admin "view as"
    impersonation — but works from a raw session mapping (``ws.session``)
    rather than a ``Request``, and returns ``None`` instead of raising when
    the caller is not signed in. The WS handler turns ``None`` into a
    ``close(4401)``.

    The returned bool is the *real* user's admin flag (impersonation never
    confers admin). The event broker uses it to bypass per-folder ACL
    filtering for admins, who can see everything.
    """
    from ..services.admin_store import is_super_admin

    s = get_settings()
    session_email = session.get("user_email") if session else None
    if s.single_user:
        email = ROOT_EMAIL
    elif s.dev_user:
        email = s.dev_user
    elif session_email:
        email = session_email
    else:
        return None

    real = _resolve_account(db, email, session)
    if is_super_admin(email) and not real.is_admin:
        stamp_person_admin(db, email, True)
    db.commit()
    real_is_admin = person_is_admin(db, email)
    effective = _cu(real)

    # Impersonation ("view as") — only honoured for a real admin.
    target_id = session.get("acting_as_user_id") if session else None
    if target_id is not None and real_is_admin:
        target = db.get(User, int(target_id))
        if target is not None:
            effective = _cu(target)

    return effective, real_is_admin


def admin_user(
    me: CurrentUser = Depends(real_user),
    db: Session = Depends(db_session),
) -> CurrentUser:
    """Real-identity admin guard for ``/api/admin/*`` routes.

    Person-level: admin on any of the email's accounts counts, so an
    admin doesn't lose the Admin button by switching to a company
    account whose row happens to lack the flag."""
    if not person_is_admin(db, me.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin only"
        )
    return me
