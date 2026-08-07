"""Impersonation endpoints (session-scoped, real-admin-only)."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ....db.models import User
from ....services import admin_store
from ....services.acl import CurrentUser
from ....services.admin_scope import AdminScope, user_in_scope
from ...deps import admin_scope, admin_user, db_session
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Impersonation (session-scoped, real-admin-only)
# ---------------------------------------------------------------------------


class ImpersonateOut(BaseModel):
    acting_as_user_id: int | None
    acting_as_email: str | None


@router.post("/impersonate/{user_id}", response_model=ImpersonateOut)
async def start_impersonate(
    user_id: int,
    request: Request,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
    scope: AdminScope = Depends(admin_scope),
) -> ImpersonateOut:
    from ....services.acl import default_account_for_email, offered_accounts_for_email

    target = db.get(User, user_id)
    # Domain-scoped: a regular admin can only impersonate users within their
    # administrative domain (out-of-domain → 404, existence-hiding).
    if target is None or not user_in_scope(scope, target):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.id == me.id:
        # Pretending to be yourself is a no-op; clear instead so the UI
        # banner disappears.
        request.session.pop("acting_as_user_id", None)
        return ImpersonateOut(acting_as_user_id=None, acting_as_email=None)
    # Never land on an account the user themself can't operate as (a
    # Clerk-only user's hidden Personal anchor) — bounce to their default.
    if not any(a.id == target.id for a in offered_accounts_for_email(db, target.email)):
        target = default_account_for_email(db, target.email)
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


class _ClerkImpersonateIn(BaseModel):
    email: EmailStr
    # '' = the user's Personal account; a Clerk org id targets that
    # company account (so "View as" from a company card lands the
    # impersonation in that company's scope).
    company_id: str = ""


@router.post("/clerk/impersonate", response_model=ImpersonateOut)
async def clerk_impersonate(
    body: _ClerkImpersonateIn,
    request: Request,
    db: Session = Depends(db_session),
    me: CurrentUser = Depends(admin_user),
) -> ImpersonateOut:
    """Impersonate a Clerk-directory user — SUPER-ADMIN only.

    Unlike the by-user-id route above, the target may never have signed
    in. We resolve their admission across EVERY enabled Clerk instance and
    provision their accounts through the exact same helper the login
    callback uses (``services/acl/clerk_provision.py``) — so an
    impersonated account gets the same ``"Instance / Org"`` labels and the
    same set of org accounts login would create (previously this path used
    a single key and bare names, so a Production-only org was missed and
    two same-named orgs across instances were indistinguishable). Stop via
    the normal DELETE /impersonate.
    """
    from ....services import clerk as clerk_svc
    from ....services.acl import provision_accounts, resolve_clerk_admission
    from ....services.admin_store import is_super_admin

    if not is_super_admin(me.email):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Impersonating Clerk users requires super-admin (VOITTA_SUPER_ADMINS).",
        )
    if not admin_store.enabled_clerk_instances():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No Clerk instance is enabled."
        )

    email = str(body.email).strip().lower()
    try:
        admission = await resolve_clerk_admission(email)
    except clerk_svc.ClerkError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    if not admission.matched:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a Clerk-directory user")

    # Org membership is the credential (same rule as the sign-in gate):
    # an org-less Clerk user can't sign in, so there is nothing to view as.
    orgs = admission.orgs
    if not orgs and not admin_store.is_native_allowed(email):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That Clerk user belongs to no organization and is not "
            "natively allowed — they cannot sign in.",
        )

    by_company = provision_accounts(db, email, admission.display_name, orgs)
    personal = by_company[""]
    db.commit()

    # Land where the user themself would: an explicit company_id wins;
    # '' means "their default" — Personal only when natively allowed,
    # else the first company (the Personal anchor is never offered).
    if body.company_id:
        target = by_company.get(body.company_id)
    elif admin_store.is_native_allowed(email):
        target = personal
    else:
        target = by_company[orgs[0]["id"]]
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "That user is not a member of the requested company.",
        )
    request.session["acting_as_user_id"] = target.id
    logger.info(
        "admin: %s now viewing as clerk user %s (account=%d, company=%s)",
        me.email, email, target.id, target.company_name or "Personal",
    )
    publish_admin_state()  # new account rows may have appeared in Users
    return ImpersonateOut(acting_as_user_id=target.id, acting_as_email=email)
