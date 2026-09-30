"""In-app assistant — configuration, credentials and conversation management.

Turns themselves stream over the ``/ws/assistant`` WebSocket
(api/assistant_ws.py); these routes are the request/response surface
around them. The whole ``/api/assistant`` prefix is session-cookie only
(api/deps.py ``_COOKIE_ONLY_PREFIXES``): an API key must not read chats or
manage LLM credentials.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...db.models import AssistantConversation
from ...services.acl import CurrentUser
from ...services.assistant import credentials, store
from ...services.assistant.catalog import (
    EFFORTS,
    ENGINE_IDS,
    ENGINE_LABELS,
    ENGINE_SHORT_LABELS,
    MODELS,
    CredentialKind,
    EngineId,
)
from ...services.assistant.identity import AssistantIdentity, ConversationView
from ...services.assistant.policy import load_policy, save_policy
from ..deps import current_user, db_session, real_user

router = APIRouter(prefix="/assistant", tags=["assistant"])


def assistant_identity(
    real: CurrentUser = Depends(real_user),
    effective: CurrentUser = Depends(current_user),
) -> AssistantIdentity:
    return AssistantIdentity(real=real, effective=effective)


def _require_admin(ident: AssistantIdentity) -> None:
    if not ident.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin only")


def engine_availability(
    db: Session, ident: AssistantIdentity, engine: EngineId
) -> tuple[bool, str | None]:
    """Whether ``ident`` can start a turn on ``engine`` now, and why not."""
    if engine == "claude_subscription" and not ident.can_use_subscription:
        return False, "The Claude subscription is available to super-admins only."
    if credentials.resolve(db, ident, engine) is None:
        if engine == "claude_subscription":
            return False, "No Claude subscription token is configured."
        return False, "No Anthropic API key is configured."
    return True, None


# --- configuration ---------------------------------------------------------


@router.get("/config")
def get_config(
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> dict:
    policy = load_policy()
    engines = []
    for engine in ENGINE_IDS:
        available, reason = engine_availability(db, ident, engine)
        engines.append(
            {
                "id": engine,
                "label": ENGINE_LABELS[engine],
                "short": ENGINE_SHORT_LABELS[engine],
                "available": available,
                "reason": reason,
            }
        )
    return {
        "enabled": policy.enabled,
        "is_admin": ident.is_admin,
        "impersonating": ident.impersonating,
        "real": {"id": ident.real.id, "email": ident.real.email},
        "effective": {"id": ident.effective.id, "email": ident.effective.email},
        "policy": policy.as_dict(),
        "models": [{"id": m.id, "label": m.label, "short": m.short} for m in MODELS],
        "efforts": list(EFFORTS),
        "engines": engines,
        "credentials": credentials.status(db, ident),
    }


class PolicyIn(BaseModel):
    enabled: bool | None = None
    default_model: str | None = None
    default_effort: str | None = None


@router.patch("/policy")
def update_policy(
    body: PolicyIn,
    ident: AssistantIdentity = Depends(assistant_identity),
) -> dict:
    _require_admin(ident)
    try:
        return save_policy(**body.model_dump()).as_dict()
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


# --- credentials -----------------------------------------------------------

CredentialScope = Literal["deployment", "person"]


class SecretIn(BaseModel):
    secret: str = Field(min_length=1, max_length=4096)


@router.put("/credentials/{scope}/{kind}")
def put_credential(
    scope: CredentialScope,
    kind: CredentialKind,
    body: SecretIn,
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> dict:
    try:
        result = credentials.store(db, ident, scope, kind, body.secret)
    except credentials.CredentialForbidden as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    except credentials.CredentialError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    db.commit()
    return result.as_dict()


@router.delete("/credentials/{scope}/{kind}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    scope: CredentialScope,
    kind: CredentialKind,
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> None:
    try:
        credentials.delete(db, ident, scope, kind)
    except credentials.CredentialForbidden as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    db.commit()


@router.post("/credentials/{scope}/{kind}/test")
async def test_credential(
    scope: CredentialScope,
    kind: CredentialKind,
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> dict:
    """Probe the stored credential against the provider.

    ``inconclusive`` (provider unreachable, timeout) leaves the stored
    verification state untouched: it says nothing about the credential.
    """
    try:
        cred = credentials.for_probe(db, ident, scope, kind)
    except credentials.CredentialForbidden as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such credential is configured")
    result, detail = await credentials.probe(cred)
    if result != "inconclusive":
        credentials.record_verification(
            db, cred.credential_id, None if result == "ok" else (detail or "rejected")
        )
        db.commit()
    return {"result": result, "detail": detail}


# --- conversations ---------------------------------------------------------


def load_accessible_conversation(
    db: Session, ident: AssistantIdentity, conversation_id: int
) -> AssistantConversation:
    """The conversation, if it lives in the caller's own list or (while
    impersonating) the impersonated account's; 404 otherwise, so foreign
    ids aren't probeable."""
    conv = store.get_conversation(db, conversation_id)
    if conv is None or not ident.can_access_owner(conv.owner_user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


@router.get("/conversations")
def list_conversations(
    view: ConversationView = Query(default="mine"),
    archived: bool = Query(default=False),
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> list[dict]:
    rows = store.list_conversations(
        db, ident.owner_for(view), include_archived=archived
    )
    return [store.conversation_out(c) for c in rows]


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> dict:
    conv = load_accessible_conversation(db, ident, conversation_id)
    return {
        "conversation": store.conversation_out(conv),
        "messages": [store.message_out(m) for m in store.messages(db, conv.id)],
    }


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=store.TITLE_MAX)
    archived: bool | None = None


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: int,
    body: ConversationPatch,
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> dict:
    conv = load_accessible_conversation(db, ident, conversation_id)
    store.update_conversation(db, conv, title=body.title, archived=body.archived)
    db.commit()
    return store.conversation_out(conv)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(db_session),
    ident: AssistantIdentity = Depends(assistant_identity),
) -> None:
    conv = load_accessible_conversation(db, ident, conversation_id)
    store.delete_conversation(db, conv)
    db.commit()
