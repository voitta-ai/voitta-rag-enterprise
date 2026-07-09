"""Personal API keys — mint, verify, CRUD, and the WS keys plane.

Token format: ``vk_<43 url-safe base64 chars>`` → ~256 bits of entropy.
The full token is shown to the user exactly once at creation time. We
persist only its SHA-256 hash plus a short prefix for UI display.

Scoping model: one key belongs to one ACCOUNT row (``ApiKey.user_id`` →
``users.id``, which is an (email, company_id) pair). The key therefore IS
the account selector for MCP requests — a key minted while a company
account was active stays scoped to that account. ``identity_for_token``
is the single seam that turns a bearer token into that identity.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.database import session_scope
from ...db.models import ApiKey, User
from ...services import events
from ...services.acl import CurrentUser
from ..deps import current_user, db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

KEY_TOKEN_PREFIX = "vk_"
KEY_DISPLAY_PREFIX_CHARS = 10  # "vk_" + 7 random chars — enough to disambiguate
MAX_KEY_NAME_LEN = 80


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> tuple[str, str, str]:
    """Generate a fresh API key.

    Returns ``(token, prefix, key_hash)``. ``token`` is the only ever
    plaintext copy — call sites must surface it to the user immediately
    and must never log it.
    """
    body = secrets.token_urlsafe(32)
    token = f"{KEY_TOKEN_PREFIX}{body}"
    prefix = token[:KEY_DISPLAY_PREFIX_CHARS]
    return token, prefix, _hash_token(token)


def verify_token(db: Session, token: str) -> ApiKey | None:
    """Look up an API key by its plaintext token; bump ``last_used_at``.

    Returns the ``ApiKey`` row when the token matches, ``None`` otherwise.
    Exposed at module level so the MCP auth flow can import it without
    depending on this whole router. The caller commits.
    """
    if not token or not token.startswith(KEY_TOKEN_PREFIX):
        return None
    key = db.execute(
        select(ApiKey).where(ApiKey.key_hash == _hash_token(token))
    ).scalar_one_or_none()
    if key is None:
        return None
    key.last_used_at = int(time.time())
    return key


def identity_for_token(db: Session, bearer: str) -> tuple[str, int] | None:
    """Resolve a bearer token to ``(email, account_id)``, or None.

    The account id is the ACCOUNT the key was minted under — that, not the
    email, drives every visibility filter downstream (MCP folder scoping).
    Bumps ``last_used_at`` as a side effect; the caller commits.

    This is the single token→identity seam: MCP's BearerAuthMiddleware is a
    thin caller, and any future change to what a key can represent (e.g.
    org-scoped keys) happens here and in the ``ApiKey`` model, not in
    middleware guts.
    """
    key = verify_token(db, bearer)
    if key is None:
        return None
    user = db.get(User, key.user_id)
    if user is None:
        # Defensive: orphaned key (user row gone).
        return None
    return (user.email, user.id)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    created_at: int
    last_used_at: int | None


class ApiKeyCreatedOut(ApiKeyOut):
    """Returned on POST — includes the plaintext ``token`` exactly once."""

    token: str


class ApiKeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_KEY_NAME_LEN)


def _to_out(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        prefix=k.prefix,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
    )


def build_keys_state(db: Session, user_id: int) -> list[dict]:
    """A user's API keys, newest first — the shape the settings modal renders.

    Feeds both the WS connect snapshot and the on-mutation push (the plaintext
    token is never included here; it's returned only in the create response)."""
    rows = (
        db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )
        .scalars()
        .all()
    )
    return [_to_out(k).model_dump() for k in rows]


def publish_keys_state(user_id: int) -> None:
    """Push the user's key list to *their* WS connection(s).

    The ``keys.*`` plane is strictly per-user: the WS pump only delivers these
    to the connection whose ``user_id`` matches the event's."""
    with session_scope() as db:
        items = build_keys_state(db, user_id)
    events.publish(
        "keys", {"type": "keys.snapshot", "user_id": user_id, "items": items}
    )


@router.get("/keys", response_model=list[ApiKeyOut])
def list_keys(
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[ApiKeyOut]:
    """Return the signed-in user's keys, newest first."""
    rows = (
        db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )
        .scalars()
        .all()
    )
    return [_to_out(k) for k in rows]


@router.post("/keys", response_model=ApiKeyCreatedOut)
def create_key(
    body: ApiKeyCreateIn,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> ApiKeyCreatedOut:
    """Mint a new key. The plaintext ``token`` is in the response and will
    never be returned again — the UI must show a "copy now" callout.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Key name cannot be blank"
        )
    token, prefix, key_hash = mint_token()
    row = ApiKey(
        user_id=user.id,
        name=name,
        prefix=prefix,
        key_hash=key_hash,
        created_at=int(time.time()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("api_key.create user=%s id=%d name=%r", user.email, row.id, name)
    publish_keys_state(user.id)
    return ApiKeyCreatedOut(**_to_out(row).model_dump(), token=token)


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_key(
    key_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> None:
    row = db.get(ApiKey, key_id)
    if row is None or row.user_id != user.id:
        # Same response for "not yours" and "not found" — don't leak
        # whether a key id belongs to a different user.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    db.delete(row)
    db.commit()
    logger.info("api_key.delete user=%s id=%d", user.email, key_id)
    publish_keys_state(user.id)
