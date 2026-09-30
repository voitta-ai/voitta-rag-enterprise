"""``/ws/assistant`` — the assistant's streaming channel.

Handshake: foreign ``Origin`` refused before accept (api/origin.py); the
session cookie authenticates (4401 when signed out); the server greets with
``{"type": "hello", "version": PROTOCOL_VERSION}``.

Client → server frames:

``{"type": "ask", "text", "conversation_id"?, "view"?, "engine"?, "model"?,
"effort"?, "ui_context"?}``
    Start a turn. Without ``conversation_id`` a conversation is created in
    the ``view`` list ("mine" | "theirs") with the given engine/model
    (pinned for its lifetime). The socket then watches that conversation.
``{"type": "watch", "conversation_id"}``
    Receive that conversation's live events (one conversation per socket;
    replaces the previous watch). Answered with ``turn_start`` +
    ``running: true`` when a turn is in progress.
``{"type": "stop", "conversation_id"}``
    Interrupt the running turn.
``{"type": "ping"}``
    Answered with ``{"type": "pong"}``.

Server → client frames are the wire events in services/assistant/protocol.py.
Identity is fixed for the socket's lifetime; the SPA reloads the page when
impersonation starts or ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from ..db.database import get_session_factory
from ..services.assistant import store
from ..services.assistant.catalog import EngineId
from ..services.assistant.identity import AssistantIdentity, ConversationView
from ..services.assistant.protocol import PROTOCOL_VERSION, ErrorEvent, TurnStart
from ..services.assistant.turns import AskRequest, TurnRejected, TurnRunner, get_turn_runner
from .deps import resolve_ws_identity
from .origin import WS_CLOSE_FORBIDDEN_ORIGIN, websocket_origin_allowed
from .ws import WS_CLOSE_UNAUTHENTICATED

logger = logging.getLogger(__name__)

router = APIRouter()


class _AskIn(BaseModel):
    text: str = Field(max_length=64_000)
    conversation_id: int | None = None
    view: ConversationView = "mine"
    engine: EngineId | None = None
    model: str | None = None
    effort: str | None = None
    ui_context: dict[str, Any] | None = None


class _Sink:
    """Serialises sends: turn tasks and the receive loop share the socket."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    async def __call__(self, frame: dict[str, Any]) -> None:
        async with self._lock:
            await self._ws.send_text(json.dumps(frame))


def _identity(ws: WebSocket) -> AssistantIdentity | None:
    session = ws.session if "session" in ws.scope else None
    db = get_session_factory()()
    try:
        resolved = resolve_ws_identity(session, db)
    finally:
        db.close()
    if resolved is None:
        return None
    real, effective, _admin = resolved
    return AssistantIdentity(real=real, effective=effective)


def _can_access(ident: AssistantIdentity, conversation_id: int) -> bool:
    db = get_session_factory()()
    try:
        conv = store.get_conversation(db, conversation_id)
        return conv is not None and ident.can_access_owner(conv.owner_user_id)
    finally:
        db.close()


def _conversation_id(msg: dict[str, Any]) -> int | None:
    cid = msg.get("conversation_id")
    return cid if isinstance(cid, int) else None


@router.websocket("/ws/assistant")
async def assistant_ws(ws: WebSocket, runner: TurnRunner = Depends(get_turn_runner)) -> None:
    if not websocket_origin_allowed(ws):
        await ws.close(code=WS_CLOSE_FORBIDDEN_ORIGIN)
        return
    await ws.accept()
    ident = await asyncio.to_thread(_identity, ws)
    if ident is None:
        await ws.send_json({"type": "error", "message": "unauthenticated", "kind": "auth"})
        await ws.close(code=WS_CLOSE_UNAUTHENTICATED)
        return

    sink = _Sink(ws)
    # Strong references to background stops (the loop keeps only weak ones).
    stopping: set[asyncio.Task[None]] = set()
    await sink({"type": "hello", "version": PROTOCOL_VERSION})
    try:
        while True:
            try:
                msg = await ws.receive_json()
            except (ValueError, KeyError, TypeError):  # not a JSON text frame
                await sink(ErrorEvent("Frames must be JSON objects.").to_wire())
                continue
            if not isinstance(msg, dict):
                await sink(ErrorEvent("Frames must be JSON objects.").to_wire())
                continue
            kind = msg.get("type")
            if kind == "ask":
                await _handle_ask(runner, ident, sink, msg)
            elif kind == "watch":
                cid = _conversation_id(msg)
                if cid is None or not await asyncio.to_thread(_can_access, ident, cid):
                    await sink(ErrorEvent("Conversation not found.").to_wire())
                    continue
                if runner.watch(cid, sink):
                    await sink(TurnStart(cid, running=True).to_wire())
            elif kind == "stop":
                cid = _conversation_id(msg)
                if cid is not None:
                    # Background: stopping may wait out the engine's grace
                    # period, and this loop must keep serving the socket.
                    task = asyncio.create_task(runner.stop(ident, cid))
                    stopping.add(task)
                    task.add_done_callback(stopping.discard)
            elif kind == "ping":
                await sink({"type": "pong"})
            else:
                await sink(ErrorEvent(f"Unknown frame type {kind!r}.").to_wire())
    except WebSocketDisconnect:
        pass
    finally:
        runner.unwatch(sink)


async def _handle_ask(
    runner: TurnRunner, ident: AssistantIdentity, sink: _Sink, msg: dict[str, Any]
) -> None:
    try:
        body = _AskIn.model_validate({k: v for k, v in msg.items() if k != "type"})
    except ValidationError as e:
        await sink(ErrorEvent(f"Invalid ask: {e.errors(include_url=False)}").to_wire())
        return
    try:
        await runner.ask(ident, AskRequest(**body.model_dump()), sink)
    except TurnRejected as e:
        await sink(ErrorEvent(str(e), e.kind).to_wire())
