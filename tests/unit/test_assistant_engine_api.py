"""Engine A (services/assistant/engines/anthropic_api.py) against a scripted
Anthropic client — the request shape it sends and the events it emits."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
from anthropic.types.beta import (
    BetaMessage,
    BetaTextBlock,
    BetaThinkingBlock,
    BetaToolUseBlock,
    BetaUsage,
)
from pydantic import BaseModel

from voitta_rag_enterprise.services.assistant.credentials import ResolvedCredential
from voitta_rag_enterprise.services.assistant.engines.anthropic_api import (
    AnthropicApiEngine,
    build_api_messages,
    normalize_assistant_blocks,
)
from voitta_rag_enterprise.services.assistant.engines.base import TurnRequest
from voitta_rag_enterprise.services.assistant.protocol import (
    EngineDone,
    Persist,
    TextDelta,
    TextFinal,
    ToolEnd,
    ToolStart,
)
from voitta_rag_enterprise.services.assistant.store import StoredMessage
from voitta_rag_enterprise.services.assistant.tools import ToolContext, ToolOutput, ToolSpec
from voitta_rag_enterprise.services.retrieval import Viewer


class _EchoArgs(BaseModel):
    query: str


def _echo(_ctx: ToolContext, a: _EchoArgs) -> ToolOutput:
    return ToolOutput(text=f'{{"echo": "{a.query}"}}', summary="1 hit")


ECHO = ToolSpec("search", "Echo the query.", _EchoArgs, _echo)
CTX = ToolContext(Viewer(user_id=7, scope="visible"))


def _msg(mid: str, content: list[Any], stop: str) -> BetaMessage:
    return BetaMessage(
        id=mid, type="message", role="assistant", model="claude-opus-5",
        content=content, stop_reason=stop, stop_sequence=None,
        usage=BetaUsage(input_tokens=100, output_tokens=20, cache_read_input_tokens=80),
    )


def _events(message: BetaMessage) -> list[Any]:
    """The raw stream events that would have produced ``message``."""
    out: list[Any] = [SimpleNamespace(type="message_start", message=SimpleNamespace(id=message.id))]
    for i, block in enumerate(message.content):
        out.append(SimpleNamespace(type="content_block_start", index=i, content_block=block))
        if block.type == "text":
            out.append(SimpleNamespace(
                type="content_block_delta", index=i,
                delta=SimpleNamespace(type="text_delta", text=block.text),
            ))
    return out


class _Stream:
    def __init__(self, message: BetaMessage | Exception) -> None:
        self._message = message

    async def __aenter__(self) -> _Stream:
        if isinstance(self._message, Exception):
            raise self._message
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self):
        async def gen():
            for e in _events(self._message):
                yield e
        return gen()

    async def get_final_message(self) -> BetaMessage:
        return self._message


class _Client:
    """Scripted stand-in for ``anthropic.AsyncAnthropic``."""

    def __init__(self, script: list[BetaMessage | Exception]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params: Any) -> _Stream:
        # Snapshot: the engine keeps appending to the same list.
        self.calls.append({**params, "messages": list(params["messages"])})
        return _Stream(self.script.pop(0))

    async def close(self) -> None:
        self.closed = True


def _request(model: str = "claude-opus-5") -> TurnRequest:
    user = StoredMessage(1, 1, "user", [{"type": "text", "text": "find x"}], 1, 7, None, None, 0)
    return TurnRequest(
        conversation_id=1, model=model, effort="high",
        credential=ResolvedCredential("anthropic_api_key", "sk-ant-test", "deployment", 1),
        system_prompt="SYSTEM", history=[user], tools=(ECHO,), tool_context=CTX,
        max_tool_rounds=4,
    )


async def _run(client: _Client, req: TurnRequest) -> list[Any]:
    engine = AnthropicApiEngine(client_factory=lambda **_kw: client)
    return [e async for e in engine.run(req)]


async def test_tool_round_then_answer() -> None:
    client = _Client([
        _msg("m1", [
            BetaThinkingBlock(type="thinking", thinking="look it up", signature="s1"),
            BetaToolUseBlock(type="tool_use", id="tu1", name="search", input={"query": "x"}),
        ], "tool_use"),
        _msg("m2", [BetaTextBlock(type="text", text="Found **x**.")], "end_turn"),
    ])
    events = await _run(client, _request())

    persists = [e for e in events if isinstance(e, Persist)]
    assert [p.role for p in persists] == ["assistant", "tool", "assistant"]
    assert persists[0].content[0] == {"type": "thinking", "thinking": "look it up", "signature": "s1"}
    assert persists[1].content[0]["tool_use_id"] == "tu1"
    assert [type(e) for e in events if isinstance(e, ToolStart | ToolEnd)] == [ToolStart, ToolEnd]
    assert any(isinstance(e, TextDelta) and e.text == "Found **x**." for e in events)
    assert any(isinstance(e, TextFinal) and e.text == "Found **x**." for e in events)
    done = events[-1]
    assert isinstance(done, EngineDone) and done.status == "done"
    assert done.usage["input_tokens"] == 200 and done.usage["cache_read_input_tokens"] == 160
    assert client.closed

    first = client.calls[0]
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"] == {"effort": "high"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["tools"][0]["input_schema"]["required"] == ["query"]
    # Round two replays the assistant turn verbatim, then the tool results.
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][0]["type"] == "thinking"
    assert second[-1]["content"][0]["type"] == "tool_result"


async def test_no_fallbacks_for_other_models() -> None:
    client = _Client([_msg("m1", [BetaTextBlock(type="text", text="hi")], "end_turn")])
    await _run(client, _request("claude-sonnet-5"))
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]


async def test_refusal_discards_partial_output() -> None:
    client = _Client([_msg("m1", [BetaTextBlock(type="text", text="partial")], "refusal")])
    events = await _run(client, _request())
    assert not any(isinstance(e, Persist) for e in events)
    assert events[-1] == EngineDone(
        "error", "The model declined this request.", "refusal",
        usage=events[-1].usage, stop_reason="refusal",
    )


async def test_auth_error_is_classified() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.AuthenticationError(
        "bad key",
        response=httpx2.Response(401, request=request),
        body={"error": {"message": "invalid x-api-key"}},
    )
    events = await _run(_Client([error]), _request())
    assert events[-1].status == "error" and events[-1].error_kind == "auth"
    assert events[-1].error == "invalid x-api-key"


async def test_tool_round_limit() -> None:
    loop = _msg("m", [BetaToolUseBlock(type="tool_use", id="t", name="search", input={"query": "x"})], "tool_use")
    req = _request()
    req = TurnRequest(**{**req.__dict__, "max_tool_rounds": 2})
    events = await _run(_Client([loop, loop]), req)
    assert events[-1].error_kind == "limit"


def test_fallback_echo_rule() -> None:
    blocks = [
        {"type": "thinking", "thinking": "a", "signature": "s"},
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "id": "t0", "name": "search", "input": {}},
        {"type": "fallback", "from": {"model": "claude-opus-5"}, "to": {"model": "claude-opus-4-8"}},
        {"type": "thinking", "thinking": "b", "signature": "s2"},
        {"type": "text", "text": "answer"},
    ]
    assert normalize_assistant_blocks(blocks) == [
        {"type": "text", "text": "partial"},
        {"type": "thinking", "thinking": "b", "signature": "s2"},
        {"type": "text", "text": "answer"},
    ]


def test_replay_renders_context_and_missing_images(env: None) -> None:
    from voitta_rag_enterprise.db.database import init_db

    init_db()
    history = [
        StoredMessage(1, 1, "user", [
            {"type": "context", "text": "folder 'Docs'"},
            {"type": "text", "text": "q"},
        ], 1, 7, None, None, 0),
        StoredMessage(2, 2, "assistant", [
            {"type": "tool_use", "id": "t1", "name": "get_image", "input": {"image_id": 999}},
        ], None, 7, None, "tool_use", 0),
        StoredMessage(3, 3, "tool", [{
            "type": "tool_result", "tool_use_id": "t1", "is_error": False,
            "content": [
                {"type": "text", "text": "{}"},
                {"type": "image_ref", "image_id": 999, "mime": "image/png", "max_size": 512},
            ],
        }], None, 7, None, None, 0),
    ]
    # Image 999 doesn't exist: replay degrades to a text placeholder.
    messages = build_api_messages(history, ToolContext(Viewer(user_id=None)))
    assert messages[0]["content"][0]["text"] == "<screen_context>\nfolder 'Docs'\n</screen_context>"
    assert messages[2]["content"][0]["content"][1] == {
        "type": "text", "text": "[image no longer available]",
    }
