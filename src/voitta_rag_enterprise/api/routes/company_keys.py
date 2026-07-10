"""Company API keys — company-scoped tokens paired with a user email.

Token format: ``cvk_<43 url-safe base64 chars>``, hashed at rest exactly
like personal keys. Unlike a personal ``vk_`` key, a company key is not an
identity by itself: every MCP/API request must also name a user email, and
the pair resolves to that user's account **in the key's company scope**.

Wire contract (both accepted; the header wins when both are present)::

    Authorization: Bearer cvk_…
    X-Voitta-User-Email: alice@example.com

    Authorization: Bearer cvk_…:alice@example.com

Scope model: ``CompanyApiKey.company_id`` is a Clerk org id (``org_…``) or
``''`` for the native deployment space. Email verification at request time:

* native space — ``admin_store.is_email_allowed`` (block-list + allowlists);
* Clerk org — live membership check (TTL-cached, ``services.clerk``);
  block-list still trumps. If Clerk is unreachable we fall back to an
  existing (email, org) account row — someone who has signed in with the
  org before keeps working through a Clerk outage; unknown emails don't.

On success the (email, company) account row is JIT-provisioned, so the
identity shape handed to MCP is the same ``(email, account_id)`` tuple
personal keys resolve to.

Management: keys are minted from the same Settings panel as personal keys,
scoped to the caller's ACTIVE account, and gated to admins — native admins
(incl. super-admins) for any scope, Clerk org admins for their own org.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.database import session_scope
from ...db.models import CompanyApiKey, User
from ...services import admin_store
from ...services.acl import CurrentUser, get_or_create_user, person_is_admin
from ..deps import current_user, db_session
from .api_keys import MAX_KEY_NAME_LEN, _hash_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

COMPANY_KEY_PREFIX = "cvk_"
COMPANY_KEY_DISPLAY_PREFIX_CHARS = 11  # "cvk_" + 7 random chars
USER_EMAIL_HEADER = "x-voitta-user-email"


def mint_company_token() -> tuple[str, str, str]:
    """Generate a fresh company key → ``(token, prefix, key_hash)``.

    Same entropy and hashing as personal keys; only the prefix differs so
    the auth middleware can dispatch on it.
    """
    import secrets

    body = secrets.token_urlsafe(32)
    token = f"{COMPANY_KEY_PREFIX}{body}"
    return token, token[:COMPANY_KEY_DISPLAY_PREFIX_CHARS], _hash_token(token)


def is_company_bearer(bearer: str) -> bool:
    return bearer.startswith(COMPANY_KEY_PREFIX)


def split_company_bearer(bearer: str) -> tuple[str, str | None]:
    """Split ``cvk_…:email`` → (token, email). Token bodies are url-safe
    base64 and never contain ':', so the first colon is the separator."""
    if ":" in bearer:
        token, email = bearer.split(":", 1)
        return token, email.strip() or None
    return bearer, None


# ---------------------------------------------------------------------------
# Request-time identity resolution (the MCP middleware's seam).
# ---------------------------------------------------------------------------


async def resolve_company_identity(
    bearer: str, header_email: str | None
) -> tuple[str, int] | None:
    """Resolve company key + email to ``(email, account_id)``, or None.

    None covers every failure: unknown token, missing email, blocked or
    non-member email. Callers treat all of them as 401 without detail —
    don't leak which half of the pair was wrong.
    """
    token, embedded_email = split_company_bearer(bearer)
    email = (header_email or embedded_email or "").strip().lower()
    if not email or "@" not in email:
        return None

    with session_scope() as db:
        key = db.execute(
            select(CompanyApiKey).where(CompanyApiKey.key_hash == _hash_token(token))
        ).scalar_one_or_none()
        if key is None:
            return None
        key_id, company_id, company_name = key.id, key.company_id, key.company_name

    if not await _email_in_scope(email, company_id):
        logger.warning(
            "company_key auth denied: email=%s scope=%s", email, company_id or "native"
        )
        return None

    with session_scope() as db:
        user = get_or_create_user(db, email, company_id, company_name)
        row = db.get(CompanyApiKey, key_id)
        if row is not None:
            row.last_used_at = int(time.time())
        return (email, user.id)


async def _email_in_scope(email: str, company_id: str) -> bool:
    """Is ``email`` currently admitted to the key's company scope?"""
    if email in set(admin_store.list_blocked_users()):
        return False

    if not company_id:
        # Native space: same gate as sign-in (allowlists + super-admins).
        return admin_store.is_email_allowed(email)

    if admin_store.get_clerk_enabled():
        secret_key = admin_store.get_clerk_secret_key()
        if secret_key:
            from ...services import clerk as clerk_svc

            try:
                members = await clerk_svc.fetch_org_members(secret_key, company_id)
                return email in members
            except clerk_svc.ClerkError as e:
                logger.warning(
                    "company_key auth: Clerk membership check failed for %s: %s",
                    company_id, e,
                )

    # Clerk disabled/unconfigured/unreachable: honour accounts that already
    # exist for this (email, org) — provisioned by a past login or a past
    # successful membership check — and reject unknown emails.
    with session_scope() as db:
        row = db.execute(
            select(User.id).where(
                User.email == email, User.company_id == company_id
            )
        ).first()
        return row is not None


# ---------------------------------------------------------------------------
# Management routes — Settings panel, admins only.
# ---------------------------------------------------------------------------


async def _require_scope_admin(db: Session, user: CurrentUser) -> None:
    """403 unless ``user`` may manage keys for their ACTIVE account's scope.

    Native admins (incl. super-admins, whose flag is stamped at login) pass
    for any scope. For an org scope, a Clerk org role of ``admin`` also
    passes — checked live (TTL-cached) so a demotion in Clerk takes effect
    within minutes, and failing CLOSED when Clerk can't answer.
    """
    if person_is_admin(db, user.email):
        return
    if user.company_id and admin_store.get_clerk_enabled():
        secret_key = admin_store.get_clerk_secret_key()
        if secret_key:
            from ...services import clerk as clerk_svc

            try:
                members = await clerk_svc.fetch_org_members(
                    secret_key, user.company_id
                )
            except clerk_svc.ClerkError as e:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Could not verify organization role with Clerk: {e}",
                ) from e
            if members.get(user.email.lower()) == "admin":
                return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN, "Company key management is admin-only"
    )


class CompanyKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    created_by: str
    created_at: int
    last_used_at: int | None


class CompanyKeyCreatedOut(CompanyKeyOut):
    """Returned on POST — includes the plaintext ``token`` exactly once."""

    token: str


class CompanyKeysOut(BaseModel):
    company_id: str
    company_name: str
    keys: list[CompanyKeyOut]


class CompanyKeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_KEY_NAME_LEN)


def _to_out(k: CompanyApiKey) -> CompanyKeyOut:
    return CompanyKeyOut(
        id=k.id,
        name=k.name,
        prefix=k.prefix,
        created_by=k.created_by,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
    )


def _scope_name(user: CurrentUser) -> str:
    return user.company_name if user.company_id else "Native space"


@router.get("/company-keys", response_model=CompanyKeysOut)
async def list_company_keys(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> CompanyKeysOut:
    """Keys for the caller's active company scope, newest first. 403 for
    non-admins — the settings UI uses that to hide the whole section."""
    await _require_scope_admin(db, user)
    rows = (
        db.execute(
            select(CompanyApiKey)
            .where(CompanyApiKey.company_id == user.company_id)
            .order_by(CompanyApiKey.created_at.desc(), CompanyApiKey.id.desc())
        )
        .scalars()
        .all()
    )
    return CompanyKeysOut(
        company_id=user.company_id,
        company_name=_scope_name(user),
        keys=[_to_out(k) for k in rows],
    )


@router.post("/company-keys", response_model=CompanyKeyCreatedOut)
async def create_company_key(
    body: CompanyKeyCreateIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> CompanyKeyCreatedOut:
    """Mint a key for the caller's active company scope. The plaintext
    ``token`` is in the response and will never be returned again."""
    await _require_scope_admin(db, user)
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Key name cannot be blank")
    token, prefix, key_hash = mint_company_token()
    row = CompanyApiKey(
        company_id=user.company_id,
        company_name=user.company_name,
        name=name,
        prefix=prefix,
        key_hash=key_hash,
        created_by=user.email,
        created_at=int(time.time()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "company_key.create by=%s scope=%s id=%d name=%r",
        user.email, user.company_id or "native", row.id, name,
    )
    return CompanyKeyCreatedOut(**_to_out(row).model_dump(), token=token)


@router.delete(
    "/company-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_company_key(
    key_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    await _require_scope_admin(db, user)
    row = db.get(CompanyApiKey, key_id)
    if row is None or row.company_id != user.company_id:
        # Same response for "other scope" and "not found" — don't leak
        # which key ids exist outside the caller's scope.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    db.delete(row)
    db.commit()
    logger.info(
        "company_key.delete by=%s scope=%s id=%d",
        user.email, user.company_id or "native", key_id,
    )
