"""The /ws event pump must end when the peer closes.

Regression: ``_pump`` never called ``receive()`` (the client sends nothing after
``subscribe``), so Starlette never saw the close, ``client_state`` stayed
CONNECTED, and the pump idled forever. Every dropped tab leaked a handler, and
on SIGTERM uvicorn waited on it indefinitely ("Waiting for background tasks to
complete") — the server could only be stopped with SIGKILL.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocket, WebSocketState

from voitta_rag_enterprise.api import ws as ws_mod
from voitta_rag_enterprise.services import events


def _socket() -> tuple[WebSocket, asyncio.Queue, list[dict]]:
    inbox: asyncio.Queue = asyncio.Queue()
    sent: list[dict] = []

    async def receive() -> dict:
        return await inbox.get()

    async def send(message: dict) -> None:
        sent.append(message)

    sock = WebSocket({"type": "websocket", "path": "/ws", "headers": []}, receive, send)
    # Past the handshake: what ws_endpoint hands to _pump.
    sock.client_state = WebSocketState.CONNECTED
    sock.application_state = WebSocketState.CONNECTED
    return sock, inbox, sent


async def _finishes(task: asyncio.Task, timeout: float = 2.0) -> None:
    """Assert ``task`` completes ON ITS OWN within ``timeout``.

    Deliberately not ``wait_for``: that cancels on timeout, and the pump swallows
    CancelledError and returns — which would make a hung pump look finished.
    """
    done, _ = await asyncio.wait({task}, timeout=timeout)
    try:
        assert task in done, "pump did not exit after the peer closed"
    finally:
        task.cancel()
    task.result()


@pytest.fixture(autouse=True)
def _short_idle(monkeypatch):
    # Keep the idle wake-up short so a regression fails fast instead of hanging.
    monkeypatch.setattr(ws_mod, "IDLE_TIMEOUT", 0.2)


async def test_pump_returns_promptly_when_peer_closes():
    sock, inbox, _ = _socket()
    async with events.subscribe(["jobs"], is_admin=True) as sub:
        pump = asyncio.create_task(ws_mod._pump(sock, sub))
        await asyncio.sleep(0.5)  # idle for a couple of IDLE_TIMEOUT cycles
        assert not pump.done()
        await inbox.put({"type": "websocket.disconnect", "code": 1012})
        await _finishes(pump)  # old code: never returns


async def test_pump_ignores_stray_client_frames_and_still_delivers():
    sock, inbox, sent = _socket()
    async with events.subscribe(["jobs"], is_admin=True) as sub:
        pump = asyncio.create_task(ws_mod._pump(sock, sub))
        await inbox.put({"type": "websocket.receive", "text": '{"type": "ping"}'})
        sub.deliver({"type": "job.updated", "job": {"id": 1}})
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.05)
        assert sent and json.loads(sent[0]["text"])["type"] == "job.updated"
        assert not pump.done()
        await inbox.put({"type": "websocket.disconnect", "code": 1000})
        await _finishes(pump)


async def test_pump_cancellation_leaves_no_dangling_reader():
    sock, _inbox, _ = _socket()
    async with events.subscribe(["jobs"], is_admin=True) as sub:
        pump = asyncio.create_task(ws_mod._pump(sock, sub))
        await asyncio.sleep(0.1)
        pump.cancel()
        await asyncio.wait_for(asyncio.gather(pump, return_exceptions=True), timeout=2)
    await asyncio.sleep(0)
    leftovers = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and "_wait_disconnect" in repr(t.get_coro())
    ]
    assert not leftovers
