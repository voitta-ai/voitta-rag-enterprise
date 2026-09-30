"""Engine B (services/assistant/engines/claude_subscription.py): isolation
settings, the tool bridge, and message → event mapping against a scripted
SDK client (no CLI process is spawned)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import BaseModel

from voitta_rag_enterprise.services.assistant.credentials import ResolvedCredential
from voitta_rag_enterprise.services.assistant.engines.base import TurnRequest
from voitta_rag_enterprise.services.assistant.engines.claude_subscription import (
    ClaudeSubscriptionEngine,
    _ToolBridge,
    engine_env,
)
from voitta_rag_enterprise.services.assistant.protocol import (
    EngineDone,
    Persist,
    SdkSession,
    TextDelta,
    ToolEnd,
    ToolStart,
)
from voitta_rag_enterprise.services.assistant.store import StoredMessage
from voitta_rag_enterprise.services.assistant.tools import (
    ToolContext,
    ToolImage,
    ToolOutput,
    ToolSpec,
)
from voitta_rag_enterprise.services.retrieval import Viewer

TOKEN = "sk-ant-oat01-" + "t" * 40
CTX = ToolContext(Viewer(user_id=3, scope="visible"))


class _Args(BaseModel):
    image_id: int


def _look(_ctx: ToolContext, a: _Args) -> ToolOutput:
    return ToolOutput(
        text=f'{{"image_id": {a.image_id}}}',
        summary="image",
        images=[ToolImage(a.image_id, "image/png", "iVBORw0KGgo=", 512)],
    )


LOOK = ToolSpec("get_image", "Look at an image.", _Args, _look)


def test_env_blanks_ambient_credentials(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    e = engine_env(TOKEN)
    assert e["ANTHROPIC_API_KEY"] == "" and e["ANTHROPIC_BASE_URL"] == "" and e["CLAUDECODE"] == ""
    assert "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK" not in e  # SDK's own knobs untouched
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert e["CLAUDE_CONFIG_DIR"].endswith("assistant/claude")
    assert e["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


async def test_bridge_returns_mcp_content_and_records_output() -> None:
    bridge = _ToolBridge((LOOK,), CTX)
    result = await bridge._handler(LOOK)({"image_id": 9})
    assert result["is_error"] is False
    assert result["content"][1] == {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
    assert bridge.allowed() == ["mcp__voitta__get_image"]
    assert next(iter(bridge.outputs.values())).images[0].image_id == 9


class _Client:
    """Scripted ClaudeSDKClient. Replays ``messages``; to exercise the image
    path it runs the real bridge handler the CLI would have called."""

    def __init__(self, options: Any, messages: list[Any]) -> None:
        self.options = options
        self._messages = messages
        self.prompt: str | None = None
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self) -> AsyncIterator[Any]:
        server = self.options.mcp_servers["voitta"]["instance"]
        for m in self._messages:
            if isinstance(m, UserMessage):
                # The CLI runs the MCP tool before reporting its result.
                await _call_tool(server, "get_image", {"image_id": 9})
            yield m

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True


async def _call_tool(server: Any, name: str, args: dict[str, Any]) -> None:
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = server.request_handlers[CallToolRequest]
    await handler(CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=args)))


def _request() -> TurnRequest:
    user = StoredMessage(1, 1, "user", [
        {"type": "context", "text": "folder 'Docs'"}, {"type": "text", "text": "show me"},
    ], 1, 3, None, None, 0)
    return TurnRequest(
        conversation_id=1, model="claude-opus-5", effort="high",
        credential=ResolvedCredential("claude_oauth_token", TOKEN, "deployment", 1),
        system_prompt="SYSTEM", history=[user], tools=(LOOK,), tool_context=CTX,
        max_tool_rounds=5, sdk_session_id="sess-1",
    )


async def test_turn_maps_messages_to_events(env: None) -> None:
    script = [
        SystemMessage(subtype="init", data={"session_id": "sess-2"}),
        StreamEvent(uuid="u", session_id="sess-2", event={"type": "message_start", "message": {"id": "msg_a"}}),
        StreamEvent(uuid="u", session_id="sess-2", event={
            "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Look"},
        }),
        AssistantMessage(
            content=[TextBlock("Looking."), ToolUseBlock("tu1", "mcp__voitta__get_image", {"image_id": 9})],
            model="claude-opus-5", message_id="msg_a", session_id="sess-2",
        ),
        UserMessage(content=[ToolResultBlock("tu1", [{"type": "text", "text": "{}"}], False)]),
        AssistantMessage(content=[TextBlock("A chart.")], model="claude-opus-5", message_id="msg_b"),
        ResultMessage(
            subtype="success", duration_ms=5, duration_api_ms=4, is_error=False, num_turns=2,
            session_id="sess-2", total_cost_usd=0.01, usage={"input_tokens": 10},
        ),
    ]
    holder: dict[str, _Client] = {}

    def factory(options: Any) -> _Client:
        holder["c"] = _Client(options, script)
        return holder["c"]

    events = [e async for e in ClaudeSubscriptionEngine(factory).run(_request())]
    client = holder["c"]

    opts = client.options
    assert opts.tools == [] and opts.strict_mcp_config and opts.setting_sources == []
    assert opts.resume == "sess-1" and opts.allowed_tools == ["mcp__voitta__get_image"]
    assert "Bash" in opts.disallowed_tools
    assert client.prompt == "<screen_context>\nfolder 'Docs'\n</screen_context>\n\nshow me"
    assert client.disconnected

    assert any(isinstance(e, SdkSession) and e.session_id == "sess-2" for e in events)
    assert any(isinstance(e, TextDelta) and e.seg == "msg_a:0" for e in events)
    start = next(e for e in events if isinstance(e, ToolStart))
    assert start.name == "get_image"  # MCP prefix stripped
    end = next(e for e in events if isinstance(e, ToolEnd))
    assert end.image_ids == [9]
    persists = [e for e in events if isinstance(e, Persist)]
    assert [p.role for p in persists] == ["assistant", "tool", "assistant"]
    assert persists[1].content[0]["content"][1] == {
        "type": "image_ref", "image_id": 9, "mime": "image/png", "max_size": 512,
    }
    done = events[-1]
    assert isinstance(done, EngineDone) and done.status == "done"
    assert done.usage["cost_usd"] == 0.01


async def test_auth_failure_is_classified(env: None) -> None:
    script = [
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
            session_id="s", result="Failed to authenticate. API Error: 401 OAuth access token is invalid.",
            api_error_status=401,
        ),
    ]
    events = [
        e async for e in ClaudeSubscriptionEngine(lambda o: _Client(o, script)).run(_request())
    ]
    assert events[-1].status == "error" and events[-1].error_kind == "auth"
