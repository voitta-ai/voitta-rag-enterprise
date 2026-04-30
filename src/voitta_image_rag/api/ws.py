"""WebSocket endpoint — clients receive live state updates over a topic stream.

v1 is server→client after the initial handshake. The client's only inbound
message is the opening ``subscribe``; subsequent messages from the client
are ignored at the application layer (starlette/uvicorn handle the
WebSocket-level ping/pong).

The pump drains the subscription's coalesced buffer in batches and sends
the events as a single JSON array per ``WebSocket.send_text`` call. Under
heavy indexing this gives us two important properties:

* one ``send_text`` per scheduling tick instead of N — tens of thousands
  of events become a much smaller number of TCP frames, and the loop
  spends less time blocked on socket writability;
* the buffer dedupes ``file.upserted`` / ``job.*`` per id, so the client
  only sees one final state per resource even if upstream produced 30.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..services import events

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_TOPICS = ("folders", "files", "jobs", "stats")

# How long the pump idles between drain checks when nothing is happening.
# Far longer than the old 100ms — the wakeup Event short-circuits this on
# any actual publish, so a higher value just means less CPU when idle.
IDLE_TIMEOUT = 5.0
# Cap per drain — keeps any single send small enough to not stall the
# socket for too long, but large enough that the typical burst leaves in
# one shot.
MAX_BATCH = 512


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
    """Forward coalesced events to the client until the WS disconnects.

    Each iteration: wait for a publish (or for IDLE_TIMEOUT to expire so we
    can recheck client_state and let the loop schedule pings), then drain
    everything buffered into a single ``batch`` frame.
    """
    while ws.client_state == WebSocketState.CONNECTED:
        try:
            await sub.wait(timeout=IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        events_out = sub.drain(max_events=MAX_BATCH)
        if not events_out:
            continue
        payload = (
            events_out[0]
            if len(events_out) == 1
            else {"type": "batch", "events": events_out}
        )
        try:
            # send_text + json.dumps so we control framing and avoid
            # starlette's default helper which dumps every event.
            await ws.send_text(json.dumps(payload))
        except (WebSocketDisconnect, RuntimeError):
            return
