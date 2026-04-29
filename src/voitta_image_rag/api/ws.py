"""WebSocket endpoint — clients receive live state updates over a topic stream.

v1 is server→client after the initial handshake. The client's only inbound
message is the opening ``subscribe``; subsequent messages from the client are
ignored at the application layer (starlette/uvicorn handle protocol-level
pings). Bidirectional commands (``search``/``cancel``) are deferred to a later
stage where they can share infrastructure with the REST search endpoint.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..services import events

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_TOPICS = ("folders", "files", "jobs", "stats")
POLL_INTERVAL = 0.1


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    try:
        first = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        return

    if first.get("type") != "subscribe":
        try:
            await ws.send_json(
                {"type": "error", "message": "first message must be subscribe"}
            )
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass
        return

    topics = [t for t in (first.get("topics") or VALID_TOPICS) if t in VALID_TOPICS]
    if not topics:
        try:
            await ws.send_json({"type": "error", "message": "no valid topics"})
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass
        return

    await ws.send_json({"type": "subscribed", "topics": list(topics)})

    async with events.subscribe(topics) as sub:
        try:
            await _pump(ws, sub)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return


async def _pump(ws: WebSocket, sub: events.Subscription) -> None:
    """Forward queued events to the client until the WS disconnects.

    Detects disconnect by polling ``ws.client_state`` between queue waits, and
    by catching ``WebSocketDisconnect`` from any ``send_json`` after the peer
    closes.
    """
    while ws.client_state == WebSocketState.CONNECTED:
        try:
            event = await asyncio.wait_for(sub.queue.get(), timeout=POLL_INTERVAL)
        except TimeoutError:
            continue
        try:
            await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            return
