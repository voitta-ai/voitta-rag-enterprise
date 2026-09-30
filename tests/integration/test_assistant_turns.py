"""TurnRunner + ``/ws/assistant`` with a scripted engine.

Covers the turn lifecycle end to end — WS protocol, persistence, identity
(author vs acting account, tools under the effective viewer), Stop with
sealing of dangling tool calls, and the rejections (no credential, busy,
foreign conversation, subscription for non-admins).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.services.acl import CurrentUser, get_or_create_user
from voitta_rag_enterprise.services.assistant import store
from voitta_rag_enterprise.services.assistant.engines.base import TurnRequest
from voitta_rag_enterprise.services.assistant.identity import AssistantIdentity
from voitta_rag_enterprise.services.assistant.protocol import (
    EngineDone,
    EngineEvent,
    Persist,
    TextDelta,
    TextFinal,
    ToolEnd,
    ToolStart,
)
from voitta_rag_enterprise.services.assistant.tools import TOOLS_BY_NAME
from voitta_rag_enterprise.services.assistant.transcript import (
    text_block,
    tool_result_block,
    tool_use_block,
)
from voitta_rag_enterprise.services.assistant.turns import (
    AskRequest,
    TurnRejected,
    TurnRunner,
    get_turn_runner,
)

API_KEY = "sk-ant-api03-" + "k" * 40


class ScriptedEngine:
    """``answer``: one real tool call (list_folders) then a text answer.
    ``hang``: emits a tool call, then waits to be interrupted."""

    requests: ClassVar[list[TurnRequest]] = []

    def __init__(self, behaviour: str) -> None:
        self._behaviour = behaviour
        self._stop = asyncio.Event()

    async def interrupt(self) -> None:
        self._stop.set()

    async def run(self, req: TurnRequest) -> AsyncGenerator[EngineEvent, None]:
        ScriptedEngine.requests.append(req)
        yield Persist("assistant", [tool_use_block("t1", "list_folders", {})])
        yield ToolStart("t1", "list_folders", {})
        if self._behaviour == "hang":
            await self._stop.wait()
            yield EngineDone("interrupted")
            return
        out = await asyncio.to_thread(TOOLS_BY_NAME["list_folders"].run, req.tool_context, {})
        yield ToolEnd("t1", out.is_error, out.summary)
        yield Persist("tool", [tool_result_block("t1", [text_block(out.text)], out.is_error)])
        yield TextDelta("m:0", "Hello")
        yield TextFinal("m:0", "Hello there")
        yield Persist("assistant", [text_block("Hello there")], stop_reason="end_turn")
        yield EngineDone("done", usage={"input_tokens": 1}, stop_reason="end_turn")


def _runner(behaviour: str = "answer", max_concurrent: int = 4) -> TurnRunner:
    ScriptedEngine.requests = []
    return TurnRunner(
        lambda _engine_id: ScriptedEngine(behaviour),
        max_concurrent=max_concurrent,
        max_tool_rounds=4,
        timeout_s=10,
    )


@pytest.fixture
def keyed_app(auth_env: None, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from voitta_rag_enterprise.config import reset_settings_cache
    from voitta_rag_enterprise.main import create_app

    monkeypatch.setenv("VOITTA_ASSISTANT_API_KEY", API_KEY)
    reset_settings_cache()
    return create_app()


def _until(ws: Any, frame_type: str) -> list[dict[str, Any]]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] in (frame_type, "error"):
            return frames


def test_ask_streams_and_persists(keyed_app: FastAPI) -> None:
    keyed_app.dependency_overrides[get_turn_runner] = lambda: _runner("answer")
    with TestClient(keyed_app) as c:
        with c.websocket_connect("/ws/assistant") as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"type": "ask", "text": "What folders do I have?\nDetails."})
            frames = _until(ws, "turn_end")
        kinds = [f["type"] for f in frames]
        assert kinds[:2] == ["conversation", "turn_start"]
        assert {"tool_start", "tool_end", "text_delta", "text"} <= set(kinds)
        end = frames[-1]
        assert end["type"] == "turn_end" and end["status"] == "done"
        conv = frames[0]["conversation"]
        assert conv["title"] == "What folders do I have?"
        assert conv["engine"] == "anthropic_api" and conv["model"] == "claude-opus-5"

        body = c.get(f"/api/assistant/conversations/{conv['id']}").json()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    user = body["messages"][0]
    assert user["author_user_id"] == user["acting_user_id"] == conv["owner_user_id"]
    # The engine got the deployment env key and a "visible"-scoped viewer.
    req = ScriptedEngine.requests[0]
    assert req.credential.source == "environment"
    assert req.tool_context.viewer.scope == "visible"


def test_stop_seals_dangling_tool_use(keyed_app: FastAPI) -> None:
    keyed_app.dependency_overrides[get_turn_runner] = lambda: _runner("hang")
    with TestClient(keyed_app) as c:
        with c.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json({"type": "ask", "text": "wait"})
            frames = _until(ws, "turn_start")
            cid = frames[-1]["conversation_id"]
            ws.send_json({"type": "stop", "conversation_id": cid})
            end = _until(ws, "turn_end")[-1]
        assert end["status"] == "interrupted"
        messages = c.get(f"/api/assistant/conversations/{cid}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "notice"]
    sealed = messages[2]["content"][0]
    assert sealed["tool_use_id"] == "t1" and sealed["is_error"] is True
    assert messages[3]["content"] == [{"type": "notice", "kind": "interrupted", "text": "Stopped."}]


def test_rejections(auth_env: None) -> None:
    from voitta_rag_enterprise.main import create_app

    app = create_app()  # no credential configured anywhere
    app.dependency_overrides[get_turn_runner] = lambda: _runner()
    with TestClient(app) as c, c.websocket_connect("/ws/assistant") as ws:
        ws.receive_json()
        ws.send_json({"type": "ask", "text": "hi"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["kind"] == "auth"
        ws.send_json({"type": "ask", "text": "   "})
        assert ws.receive_json()["message"] == "The message is empty."
        ws.send_json({"type": "watch", "conversation_id": 12345})
        assert ws.receive_json()["message"] == "Conversation not found."
        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["type"] == "error"


def _user(email: str) -> CurrentUser:
    with session_scope() as s:
        u = get_or_create_user(s, email)
        return CurrentUser(id=u.id, email=u.email)


async def _ask_and_wait(runner: TurnRunner, ident: AssistantIdentity, req: AskRequest) -> list[dict]:
    frames: list[dict] = []
    done = asyncio.Event()

    async def sink(frame: dict) -> None:
        frames.append(frame)
        if frame["type"] == "turn_end":
            done.set()

    await runner.ask(ident, req, sink)
    await asyncio.wait_for(done.wait(), 5)
    return frames


async def test_impersonated_turn_runs_as_effective_account(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_ASSISTANT_API_KEY", API_KEY)
    reset_settings_cache()
    init_db()
    admin, bob = _user("root@x"), _user("bob@x")
    ident = AssistantIdentity(real=admin, effective=bob)
    runner = _runner()

    frames = await _ask_and_wait(runner, ident, AskRequest(text="status?", view="theirs"))
    cid = frames[0]["conversation"]["id"]
    assert frames[0]["conversation"]["owner_user_id"] == bob.id
    assert ScriptedEngine.requests[0].tool_context.viewer.user_id == bob.id
    with session_scope() as s:
        user_msg = store.messages(s, cid)[0]
    assert (user_msg.author_user_id, user_msg.acting_user_id) == (admin.id, bob.id)

    # Bob alone can't reach the admin's own ("mine") conversation.
    mine = await _ask_and_wait(runner, ident, AskRequest(text="mine", view="mine"))
    mine_id = mine[0]["conversation"]["id"]
    with pytest.raises(TurnRejected, match="not found"):
        await runner.ask(
            AssistantIdentity(bob, bob), AskRequest(text="x", conversation_id=mine_id), _noop
        )


async def _noop(_frame: dict) -> None:
    return None


async def test_subscription_is_admin_only(env: None) -> None:
    init_db()
    bob = _user("bob@x")
    with pytest.raises(TurnRejected) as exc:
        await _runner().ask(
            AssistantIdentity(bob, bob), AskRequest(text="hi", engine="claude_subscription"), _noop
        )
    assert exc.value.kind == "auth"


async def test_concurrency_limit(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("VOITTA_ASSISTANT_API_KEY", API_KEY)
    reset_settings_cache()
    init_db()
    bob = _user("bob@x")
    ident = AssistantIdentity(bob, bob)
    runner = _runner("hang", max_concurrent=1)
    first = await runner.ask(ident, AskRequest(text="one"), _noop)
    with pytest.raises(TurnRejected) as exc:
        await runner.ask(ident, AskRequest(text="two"), _noop)
    assert exc.value.kind == "limit"
    with pytest.raises(TurnRejected, match="still being written"):
        await runner.ask(ident, AskRequest(text="again", conversation_id=first), _noop)
    await runner.stop(ident, first)
    assert not runner.is_running(first)
