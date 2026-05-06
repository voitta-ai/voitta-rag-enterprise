"""Admin-only REST surface.

All routes here are guarded by ``admin_user`` — only the *real* user's
``is_admin`` flag matters; impersonation does NOT confer admin rights.

Three concerns covered:

1. **Allowlist / blocklist editing** — the live sign-in gate's source of
   truth lives in plain text files on the data PD; see
   ``services.admin_store``.

2. **User admin status** — flip ``users.is_admin`` for any address that
   has signed in at least once. Bootstrap admins (env-listed
   ``VOITTA_SUPER_ADMINS``) cannot be demoted via this API: their flag
   gets re-stamped on every sign-in.

3. **Impersonation** — set/clear ``session["acting_as_user_id"]``. The
   ``current_user`` dependency reads that and routes the rest of the
   app at the impersonated user's permissions.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import User
from ...services import admin_store
from ...services.acl import CurrentUser
from ..deps import admin_user, db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Allowlist / blocklist
# ---------------------------------------------------------------------------


class AllowlistOut(BaseModel):
    domains: list[str]
    users: list[str]
    blocked: list[str]
    super_admins: list[str]  # read-only — derived from VOITTA_SUPER_ADMINS


class _DomainIn(BaseModel):
    domain: str


class _EmailIn(BaseModel):
    email: EmailStr


@router.get("/allowlist", response_model=AllowlistOut)
def get_allowlist(_: CurrentUser = Depends(admin_user)) -> AllowlistOut:
    from ...config import get_settings

    return AllowlistOut(
        domains=admin_store.list_allowed_domains(),
        users=admin_store.list_allowed_users(),
        blocked=admin_store.list_blocked_users(),
        super_admins=get_settings().super_admin_list(),
    )


@router.post("/allowlist/domains", response_model=AllowlistOut)
def add_domain(
    body: _DomainIn,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_allowed_domain(body.domain)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s added domain %s", me.email, body.domain)
    return get_allowlist(me)


@router.delete("/allowlist/domains/{domain}", response_model=AllowlistOut)
def remove_domain(
    domain: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_domain(domain)
    logger.info("admin: %s removed domain %s", me.email, domain)
    return get_allowlist(me)


@router.post("/allowlist/users", response_model=AllowlistOut)
def add_email(
    body: _EmailIn,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_allowed_user(str(body.email))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s allowed %s", me.email, body.email)
    return get_allowlist(me)


@router.delete("/allowlist/users/{email}", response_model=AllowlistOut)
def remove_email(
    email: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_user(email)
    logger.info("admin: %s removed allowed user %s", me.email, email)
    return get_allowlist(me)


@router.post("/blocklist", response_model=AllowlistOut)
def add_block(
    body: _EmailIn,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_blocked_user(str(body.email))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s blocked %s", me.email, body.email)
    return get_allowlist(me)


@router.delete("/blocklist/{email}", response_model=AllowlistOut)
def remove_block(
    email: str,
    me: CurrentUser = Depends(admin_user),
) -> AllowlistOut:
    admin_store.remove_blocked_user(email)
    logger.info("admin: %s unblocked %s", me.email, email)
    return get_allowlist(me)


# ---------------------------------------------------------------------------
# Users (existing User rows + admin flag)
# ---------------------------------------------------------------------------


class AdminUserOut(BaseModel):
    id: int
    email: str
    display_name: str | None
    is_admin: bool
    is_super_admin: bool


class _AdminFlagIn(BaseModel):
    is_admin: bool


class _CreateUserIn(BaseModel):
    email: EmailStr
    # Default-true: the natural intent of "add a user" via the admin
    # panel is that they can actually sign in. Operator can flip the
    # checkbox off in the UI for the rare "I want a row in the DB but
    # not on the allowlist" case (mostly: legacy data).
    grant_signin: bool = True
    is_admin: bool = False


@router.post("/users", response_model=AdminUserOut)
def create_user(
    body: _CreateUserIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> AdminUserOut:
    """Pre-create a User row and (optionally) mark them admin + allowlist.

    Why this exists: the Users table only ever showed users who had
    signed in at least once, so an admin couldn't pre-grant admin
    powers to a teammate who hadn't logged in yet. This endpoint
    creates the row up front so the flag has somewhere to live, and
    by default also adds the address to ``allowed_users.txt`` so the
    teammate can actually sign in. The admin flag is then just one
    PATCH away — or set in this same call via ``is_admin=True``.
    """
    from ...config import get_settings
    from ...services.acl import get_or_create_user

    email = str(body.email).strip().lower()
    user = get_or_create_user(db, email)
    user.is_admin = bool(body.is_admin) or bool(user.is_admin)
    db.commit()

    if body.grant_signin:
        admin_store.add_allowed_user(email)

    logger.info(
        "admin: %s pre-created %s (admin=%s, signin=%s)",
        me.email, email, body.is_admin, body.grant_signin,
    )

    super_set = {sa.lower() for sa in get_settings().super_admin_list()}
    return AdminUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=bool(user.is_admin),
        is_super_admin=user.email.lower() in super_set,
    )


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    db: Session = Depends(db_session),
    _: CurrentUser = Depends(admin_user),
) -> list[AdminUserOut]:
    """Every User row that has ever signed in. Used by the admin UI for
    both the admin-flag toggle and the impersonation dropdown."""
    from ...config import get_settings

    super_set = {sa.lower() for sa in get_settings().super_admin_list()}
    rows = db.execute(select(User).order_by(User.email)).scalars().all()
    return [
        AdminUserOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            is_admin=bool(u.is_admin),
            is_super_admin=u.email.lower() in super_set,
        )
        for u in rows
    ]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def set_admin_flag(
    user_id: int,
    body: _AdminFlagIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> AdminUserOut:
    """Toggle ``is_admin`` for any user. Super-admins can't be demoted —
    their flag re-stamps on every sign-in, so attempting to flip it
    silently sticks but won't survive their next login. We could 409
    instead but it's friendlier to let the call succeed and let the
    admin discover that side via the ``is_super_admin`` flag in the
    response."""
    from ...config import get_settings

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    target.is_admin = bool(body.is_admin)
    db.commit()
    logger.info(
        "admin: %s set is_admin=%s for %s", me.email, body.is_admin, target.email
    )
    super_set = {sa.lower() for sa in get_settings().super_admin_list()}
    return AdminUserOut(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        is_admin=bool(target.is_admin),
        is_super_admin=target.email.lower() in super_set,
    )


# ---------------------------------------------------------------------------
# Impersonation (session-scoped, real-admin-only)
# ---------------------------------------------------------------------------


class ImpersonateOut(BaseModel):
    acting_as_user_id: int | None
    acting_as_email: str | None


@router.post("/impersonate/{user_id}", response_model=ImpersonateOut)
def start_impersonate(
    user_id: int,
    request: Request,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> ImpersonateOut:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.id == me.id:
        # Pretending to be yourself is a no-op; clear instead so the UI
        # banner disappears.
        request.session.pop("acting_as_user_id", None)
        return ImpersonateOut(acting_as_user_id=None, acting_as_email=None)
    request.session["acting_as_user_id"] = target.id
    logger.info("admin: %s now viewing as %s", me.email, target.email)
    return ImpersonateOut(acting_as_user_id=target.id, acting_as_email=target.email)


@router.delete("/impersonate", response_model=ImpersonateOut)
def stop_impersonate(
    request: Request,
    _: CurrentUser = Depends(admin_user),
) -> ImpersonateOut:
    request.session.pop("acting_as_user_id", None)
    return ImpersonateOut(acting_as_user_id=None, acting_as_email=None)
