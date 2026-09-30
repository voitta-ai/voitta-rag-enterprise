"""Conversation + message persistence (append-only transcripts).

Pure repository functions over an explicit session; authorization is the
caller's job (``AssistantIdentity.can_access_owner``) — this module never
decides who may see what.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import AssistantConversation, AssistantMessage
from .catalog import EngineId
from .transcript import Block, Role

TITLE_MAX = 80


@dataclass(frozen=True)
class StoredMessage:
    id: int
    seq: int
    role: Role
    content: list[Block]
    author_user_id: int | None
    acting_user_id: int | None
    usage: dict[str, Any] | None
    stop_reason: str | None
    created_at: int


def conversation_out(c: AssistantConversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "owner_user_id": c.owner_user_id,
        "created_by_user_id": c.created_by_user_id,
        "title": c.title,
        "engine": c.engine,
        "model": c.model,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "archived": c.archived_at is not None,
    }


def message_out(m: StoredMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "seq": m.seq,
        "role": m.role,
        "content": m.content,
        "author_user_id": m.author_user_id,
        "acting_user_id": m.acting_user_id,
        "usage": m.usage,
        "stop_reason": m.stop_reason,
        "created_at": m.created_at,
    }


def create_conversation(
    db: Session,
    *,
    owner_user_id: int,
    created_by_user_id: int,
    engine: EngineId,
    model: str,
) -> AssistantConversation:
    now = int(time.time())
    conv = AssistantConversation(
        owner_user_id=owner_user_id,
        created_by_user_id=created_by_user_id,
        engine=engine,
        model=model,
        created_at=now,
        updated_at=now,
    )
    db.add(conv)
    db.flush()
    return conv


def get_conversation(db: Session, conversation_id: int) -> AssistantConversation | None:
    return db.get(AssistantConversation, conversation_id)


def list_conversations(
    db: Session, owner_user_id: int, *, include_archived: bool = False, limit: int = 100
) -> list[AssistantConversation]:
    stmt = select(AssistantConversation).where(
        AssistantConversation.owner_user_id == owner_user_id
    )
    if not include_archived:
        stmt = stmt.where(AssistantConversation.archived_at.is_(None))
    stmt = stmt.order_by(
        AssistantConversation.updated_at.desc(), AssistantConversation.id.desc()
    ).limit(limit)
    return list(db.execute(stmt).scalars())


def update_conversation(
    db: Session,
    conv: AssistantConversation,
    *,
    title: str | None = None,
    archived: bool | None = None,
) -> AssistantConversation:
    if title is not None:
        conv.title = title.strip()[:TITLE_MAX]
    if archived is not None:
        conv.archived_at = int(time.time()) if archived else None
    db.flush()
    return conv


def delete_conversation(db: Session, conv: AssistantConversation) -> None:
    db.delete(conv)
    db.flush()


def set_sdk_session(db: Session, conv: AssistantConversation, session_id: str | None) -> None:
    conv.sdk_session_id = session_id
    db.flush()


def append_message(
    db: Session,
    conv: AssistantConversation,
    *,
    role: Role,
    content: list[Block],
    author_user_id: int | None = None,
    acting_user_id: int | None = None,
    usage: dict[str, Any] | None = None,
    stop_reason: str | None = None,
) -> StoredMessage:
    """Append one message at the next ``seq`` and touch the conversation."""
    seq = (
        db.execute(
            select(func.max(AssistantMessage.seq)).where(
                AssistantMessage.conversation_id == conv.id
            )
        ).scalar_one()
        or 0
    ) + 1
    now = int(time.time())
    row = AssistantMessage(
        conversation_id=conv.id,
        seq=seq,
        role=role,
        content_json=json.dumps(content, ensure_ascii=False),
        author_user_id=author_user_id,
        acting_user_id=acting_user_id,
        usage_json=json.dumps(usage) if usage else None,
        stop_reason=stop_reason,
        created_at=now,
    )
    db.add(row)
    conv.updated_at = now
    db.flush()
    return _stored(row)


def messages(db: Session, conversation_id: int) -> list[StoredMessage]:
    rows = db.execute(
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.seq)
    ).scalars()
    return [_stored(r) for r in rows]


def _stored(row: AssistantMessage) -> StoredMessage:
    return StoredMessage(
        id=row.id,
        seq=row.seq,
        role=row.role,  # type: ignore[arg-type]
        content=json.loads(row.content_json),
        author_user_id=row.author_user_id,
        acting_user_id=row.acting_user_id,
        usage=json.loads(row.usage_json) if row.usage_json else None,
        stop_reason=row.stop_reason,
        created_at=row.created_at,
    )
