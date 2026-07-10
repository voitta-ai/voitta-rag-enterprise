"""Allowlist / blocklist editing endpoints."""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from ....services import admin_store
from ....services.acl import CurrentUser
from ...deps import admin_user, super_admin_user
from .base import publish_admin_state, router

logger = logging.getLogger(__name__)


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
    from ....config import get_settings

    return AllowlistOut(
        domains=admin_store.list_allowed_domains(),
        users=admin_store.list_allowed_users(),
        blocked=admin_store.list_blocked_users(),
        super_admins=get_settings().super_admin_list(),
    )


@router.post("/allowlist/domains", response_model=AllowlistOut)
def add_domain(
    body: _DomainIn,
    me: CurrentUser = Depends(super_admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_allowed_domain(body.domain)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s added domain %s", me.email, body.domain)
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.delete("/allowlist/domains/{domain}", response_model=AllowlistOut)
def remove_domain(
    domain: str,
    me: CurrentUser = Depends(super_admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_domain(domain)
    logger.info("admin: %s removed domain %s", me.email, domain)
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.post("/allowlist/users", response_model=AllowlistOut)
def add_email(
    body: _EmailIn,
    me: CurrentUser = Depends(super_admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_allowed_user(str(body.email))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s allowed %s", me.email, body.email)
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.delete("/allowlist/users/{email}", response_model=AllowlistOut)
def remove_email(
    email: str,
    me: CurrentUser = Depends(super_admin_user),
) -> AllowlistOut:
    admin_store.remove_allowed_user(email)
    logger.info("admin: %s removed allowed user %s", me.email, email)
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.post("/blocklist", response_model=AllowlistOut)
def add_block(
    body: _EmailIn,
    me: CurrentUser = Depends(super_admin_user),
) -> AllowlistOut:
    try:
        admin_store.add_blocked_user(str(body.email))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    logger.info("admin: %s blocked %s", me.email, body.email)
    out = get_allowlist(me)
    publish_admin_state()
    return out


@router.delete("/blocklist/{email}", response_model=AllowlistOut)
def remove_block(
    email: str,
    me: CurrentUser = Depends(super_admin_user),
) -> AllowlistOut:
    admin_store.remove_blocked_user(email)
    logger.info("admin: %s unblocked %s", me.email, email)
    out = get_allowlist(me)
    publish_admin_state()
    return out
