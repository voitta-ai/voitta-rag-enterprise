"""User admin endpoints — existing User rows + admin flag."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....db.models import User
from ....services import admin_store
from ....services.acl import CurrentUser
from ....services.admin_scope import (
    AdminScope,
    filter_users_for_scope,
    user_in_scope,
)
from ...deps import admin_scope, admin_user, db_session, super_admin_user
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Users (existing User rows + admin flag)
# ---------------------------------------------------------------------------


class AdminUserOut(BaseModel):
    # One row per ACCOUNT — (email, company_id). The UI groups rows by
    # email so a multi-account person reads as one entry with chips.
    id: int
    email: str
    company_id: str = ""
    company_name: str = ""
    display_name: str | None
    is_admin: bool
    is_super_admin: bool
    groups: list[str] = []
    # Live allowlist check (badge provenance). Display-only.
    native_allowed: bool = False


def _super_set() -> set[str]:
    from ....config import get_settings

    return {sa.lower() for sa in get_settings().super_admin_list()}


def _user_out(
    user: User,
    *,
    groups: list[str],
    supers: set[str] | None = None,
) -> AdminUserOut:
    supers = _super_set() if supers is None else supers
    return AdminUserOut(
        id=user.id,
        email=user.email,
        company_id=user.company_id or "",
        company_name=user.company_name or "",
        display_name=user.display_name,
        is_admin=bool(user.is_admin),
        is_super_admin=user.email.lower() in supers,
        groups=groups,
        native_allowed=admin_store.is_native_allowed(user.email),
    )


class _AdminFlagIn(BaseModel):
    # All optional — a PATCH may flip admin, rename, set groups, or any combo.
    # ``None`` means "leave alone"; for groups, pass [] to clear all memberships.
    is_admin: bool | None = None
    display_name: str | None = None
    groups: list[str] | None = None


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
    me: CurrentUser = Depends(super_admin_user),
) -> AdminUserOut:
    """Pre-create a User row and (optionally) mark them admin + allowlist.

    Super-admin only: ``grant_signin`` writes the native allowlist and the
    row isn't bound to any org, so this is a deployment-global action.

    Why this exists: the Users table only ever showed users who had
    signed in at least once, so an admin couldn't pre-grant admin
    powers to a teammate who hadn't logged in yet. This endpoint
    creates the row up front so the flag has somewhere to live, and
    by default also adds the address to ``allowed_users.txt`` so the
    teammate can actually sign in. The admin flag is then just one
    PATCH away — or set in this same call via ``is_admin=True``.
    """
    from ....services.acl import get_or_create_user

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

    out = _user_out(user, groups=[])
    publish_admin_state()
    return out


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    db: Session = Depends(db_session),
    scope: AdminScope = Depends(admin_scope),
) -> list[AdminUserOut]:
    """User rows within the caller's administrative domain. Superadmins see
    every account; a regular admin sees only members of the orgs they
    administer (plus native users if they're a native admin). Feeds both the
    admin-flag toggle and the impersonation dropdown."""
    from ....services import groups as groups_svc

    supers = _super_set()
    by_user = groups_svc.group_names_by_user(db)
    rows = db.execute(
        select(User).order_by(User.email, User.company_id)
    ).scalars().all()
    rows = filter_users_for_scope(scope, rows)
    return [
        _user_out(u, groups=by_user.get(u.id, []), supers=supers) for u in rows
    ]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: int,
    body: _AdminFlagIn,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
    scope: AdminScope = Depends(admin_scope),
) -> AdminUserOut:
    """Update a user: admin flag, display name, and/or group membership.

    Each field is optional ("leave alone" when omitted). Super-admins can't be
    demoted — their flag re-stamps on every sign-in, so a flip here silently
    sticks but won't survive their next login; we let the call succeed and let
    the admin see ``is_super_admin`` in the response rather than 409.

    Domain-scoped: a regular admin may only touch users in their
    administrative domain; an out-of-domain (or missing) id is 404 so foreign
    accounts aren't probeable. Note the admin-flag/display-name change is
    person-level (``stamp_person_admin`` flips every account row of the
    email) — authorization is on the *targeted* row being in-domain.
    """
    from ....services import groups as groups_svc

    target = db.get(User, user_id)
    if target is None or not user_in_scope(scope, target):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if body.is_admin is not None:
        # Person-level: the flag applies to every account of this email.
        from ....services.acl import stamp_person_admin

        stamp_person_admin(db, target.email, bool(body.is_admin))
    if body.display_name is not None:
        target.display_name = body.display_name.strip() or None
    if body.groups is not None:
        groups_svc.set_user_groups(db, user_id, body.groups)
    db.commit()
    logger.info(
        "admin: %s updated user %s (admin=%s, name=%s, groups=%s)",
        me.email, target.email, body.is_admin, body.display_name, body.groups,
    )
    out = _user_out(target, groups=groups_svc.group_names_for_user(db, user_id))
    publish_admin_state()
    return out


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
    scope: AdminScope = Depends(admin_scope),
) -> None:
    """Delete a user. Memberships / api_keys / folder_acl cascade; owned
    folders' ``owner_id`` is set null per schema. Guards: can't delete a
    super-admin (they'd just be re-created on next sign-in, and it reads as a
    footgun) nor yourself (lock-out protection); a regular admin may only
    delete users within their administrative domain (out-of-domain → 404)."""
    target = db.get(User, user_id)
    if target is None or not user_in_scope(scope, target):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.id == me.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You can't delete your own account."
        )
    if target.email.lower() in _super_set():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Super-admins (VOITTA_SUPER_ADMINS) can't be deleted here.",
        )
    email = target.email
    db.delete(target)
    db.commit()
    logger.info("admin: %s deleted user %s", me.email, email)
    publish_admin_state()
