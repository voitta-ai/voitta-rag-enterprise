"""Engine B — the Claude subscription, through the Claude Agent SDK.

The SDK drives the ``claude`` CLI it bundles, authenticated with the
deployment's shared subscription token (``claude setup-token``; usable by
super-admins only — see identity.py). One CLI process per turn; the
conversation continues across turns by resuming the SDK session whose id
the TurnRunner stores on the conversation.

Isolation from whatever else lives on the host:

* ``CLAUDE_CONFIG_DIR`` is a directory of our own under ``data_dir`` — no
  host ``~/.claude`` settings, hooks, CLAUDE.md, memory or logins; the SDK
  sessions we resume live there too. ``setting_sources=[]`` and
  ``strict_mcp_config`` keep project/user settings and foreign MCP servers
  out; claude.ai connectors are disabled.
* The SDK merges this process's whole environment into the CLI's and only
  lets ``options.env`` *override* keys, so every inherited ``ANTHROPIC_*`` /
  ``CLAUDE*`` variable is explicitly blanked — an ambient API key, base URL
  or parent-session variable must not reroute or re-bill the subscription.
* No built-in tools at all (no shell, files, web): the model gets exactly
  our read-only tools, served in-process as an SDK MCP server, and a
  ``can_use_tool`` backstop denies anything else.

The stored transcript is display/audit for this engine (the SDK session is
the model-facing history); it is written in the same block format as
engine A so the UI renders both identically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from ....config import get_settings
from ..credentials import ProbeResult
from ..protocol import (
    EngineDone,
    EngineEvent,
    ErrorKind,
    Persist,
    PhaseEvent,
    SdkSession,
    TextDelta,
    TextFinal,
    ThinkingDelta,
    ToolEnd,
    ToolStart,
)
from ..tools import MAX_RESULT_CHARS, ToolContext, ToolOutput, ToolSpec
from ..transcript import Block, image_ref_block, text_block, tool_result_block
from .base import TurnRequest

logger = logging.getLogger(__name__)

MCP_SERVER = "voitta"
_TOOL_PREFIX = f"mcp__{MCP_SERVER}__"
# Built-ins that must never run, whatever the CLI version ships.
_DENIED_BUILTINS = (
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
    "WebSearch", "WebFetch", "Agent", "Task", "TodoWrite", "Skill", "AskUserQuestion",
    "Workflow", "Artifact",
)
_AUTH_HINTS = (
    "not logged in", "please run /login", "invalid api key", "authentication",
    "unauthorized", "oauth", "expired", "invalid bearer", "credit balance",
)
PROBE_TIMEOUT_S = 75.0


def claude_home() -> Path:
    """The CLI's isolated config dir (sessions, state). Shared by all
    super-admins — they share one subscription."""
    return get_settings().data_dir / "assistant" / "claude"


def _workspace() -> Path:
    """An empty working directory: nothing for the CLI to discover."""
    return get_settings().data_dir / "assistant" / "workspace"


def engine_env(token: str) -> dict[str, str]:
    """``options.env`` for the CLI (see module doc on why keys are blanked)."""
    env = {
        k: ""
        for k in os.environ
        if k.startswith(("ANTHROPIC_", "CLAUDE")) and not k.startswith("CLAUDE_AGENT_SDK")
    }
    env.update({
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "CLAUDE_CONFIG_DIR": str(claude_home()),
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        # No auto-update, telemetry or error reporting from a server process.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    return env


def _is_auth_failure(text: str, status: int | None = None) -> bool:
    return status in (401, 403) or any(h in text.lower() for h in _AUTH_HINTS)


def _output_key(name: str, tool_input: Any) -> str:
    return f"{name}:{json.dumps(tool_input or {}, sort_keys=True, default=str)}"


class _ToolBridge:
    """Our ToolSpecs as an in-process SDK MCP server.

    The SDK hands tool results back as MCP content (no image ids), so each
    handler records its :class:`ToolOutput` under (name, input); the engine
    looks it up when the matching ``ToolResultBlock`` arrives to persist
    image references and report image ids to the UI.
    """

    def __init__(self, tools: tuple[ToolSpec, ...], ctx: ToolContext) -> None:
        self._tools = tools
        self._ctx = ctx
        self.outputs: dict[str, ToolOutput] = {}

    def server(self) -> Any:
        from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

        sdk_tools = []
        for spec in self._tools:
            handler = self._handler(spec)
            sdk_tools.append(
                tool(
                    spec.name,
                    spec.description,
                    spec.input_schema(),
                    annotations=ToolAnnotations(
                        readOnlyHint=True, maxResultSizeChars=MAX_RESULT_CHARS + 2_000
                    ),
                )(handler)
            )
        return create_sdk_mcp_server(MCP_SERVER, tools=sdk_tools)

    def allowed(self) -> list[str]:
        return [_TOOL_PREFIX + t.name for t in self._tools]

    def _handler(self, spec: ToolSpec) -> Any:
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            out = await asyncio.to_thread(spec.run, self._ctx, args)
            self.outputs[_output_key(spec.name, args)] = out
            content: list[dict[str, Any]] = [{"type": "text", "text": out.text}]
            content.extend(
                {"type": "image", "data": img.data_base64, "mimeType": img.mime}
                for img in out.images
            )
            return {"content": content, "is_error": out.is_error}

        return run


class ClaudeSubscriptionEngine:
    """``client_factory`` builds the SDK client from ``ClaudeAgentOptions``
    (injectable for tests); defaults to ``claude_agent_sdk.ClaudeSDKClient``."""

    def __init__(self, client_factory: Callable[[Any], Any] | None = None) -> None:
        self._client_factory = client_factory
        self._client: Any = None
        self._interrupting = False
        self._stderr: list[str] = []

    async def interrupt(self) -> None:
        self._interrupting = True
        if self._client is not None:
            await self._client.interrupt()

    def _options(self, req: TurnRequest, bridge: _ToolBridge) -> Any:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            PermissionResultAllow,
            PermissionResultDeny,
        )

        allowed = bridge.allowed()

        async def can_use_tool(name: str, _input: dict[str, Any], _ctx: Any) -> Any:
            if name in allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"{name} is not available here")

        for d in (claude_home(), _workspace()):
            d.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            model=req.model,
            effort=req.effort,  # type: ignore[arg-type]
            thinking={"type": "adaptive", "display": "summarized"},
            system_prompt=req.system_prompt,
            tools=[],
            mcp_servers={MCP_SERVER: bridge.server()},
            strict_mcp_config=True,
            allowed_tools=allowed,
            disallowed_tools=list(_DENIED_BUILTINS),
            can_use_tool=can_use_tool,
            setting_sources=[],
            include_partial_messages=True,
            max_turns=req.max_tool_rounds,
            resume=req.sdk_session_id,
            cwd=str(_workspace()),
            env=engine_env(req.credential.secret),
            stderr=self._stderr.append,
        )

    async def run(self, req: TurnRequest) -> AsyncGenerator[EngineEvent, None]:
        from claude_agent_sdk import ClaudeSDKClient, CLINotFoundError

        prompt = _prompt_text(req)
        bridge = _ToolBridge(req.tools, req.tool_context)
        client = (self._client_factory or ClaudeSDKClient)(self._options(req, bridge))
        try:
            await client.connect()
        except CLINotFoundError as e:
            yield EngineDone("error", f"Claude Code is not available: {e}", "unavailable")
            return
        except Exception as e:
            yield self._failure(f"Could not start Claude Code: {e}")
            return
        self._client = client
        try:
            async for event in self._turn(client, prompt, bridge):
                yield event
        finally:
            self._client = None
            try:
                await asyncio.wait_for(client.disconnect(), 10)
            except Exception:
                logger.warning("Claude Code did not disconnect cleanly", exc_info=True)

    async def _turn(
        self, client: Any, prompt: str, bridge: _ToolBridge
    ) -> AsyncGenerator[EngineEvent, None]:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        tool_uses: dict[str, tuple[str, Any]] = {}
        announced: str | None = None
        message_id = "m"
        yield PhaseEvent("thinking")
        await client.query(prompt)
        try:
            async for message in client.receive_response():
                sid = getattr(message, "session_id", None)
                if sid and sid != announced:
                    announced = sid
                    yield SdkSession(sid)
                if isinstance(message, StreamEvent):
                    for event in self._stream_event(message.event, message_id):
                        if isinstance(event, str):
                            message_id = event
                        else:
                            yield event
                elif isinstance(message, AssistantMessage):
                    blocks: list[Block] = []
                    for index, block in enumerate(message.content):
                        if isinstance(block, TextBlock):
                            blocks.append(text_block(block.text))
                            yield TextFinal(f"{message.message_id or message_id}:{index}", block.text)
                        elif isinstance(block, ThinkingBlock):
                            blocks.append({
                                "type": "thinking",
                                "thinking": block.thinking,
                                "signature": block.signature,
                            })
                        elif isinstance(block, ToolUseBlock):
                            name = block.name.removeprefix(_TOOL_PREFIX)
                            tool_uses[block.id] = (name, block.input)
                            blocks.append({
                                "type": "tool_use", "id": block.id, "name": name, "input": block.input,
                            })
                            yield ToolStart(block.id, name, dict(block.input))
                    if blocks:
                        yield Persist(
                            "assistant", blocks, usage=message.usage, stop_reason=message.stop_reason
                        )
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    results: list[Block] = []
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            stored, event = self._tool_result(block, tool_uses, bridge)
                            results.append(stored)
                            yield event
                    if results:
                        yield Persist("tool", results)
                        yield PhaseEvent("thinking")
                elif isinstance(message, ResultMessage):
                    yield self._done(message)
                    return
        except Exception as e:
            if self._interrupting:
                yield EngineDone("interrupted")
            else:
                yield self._failure(str(e))
            return
        yield EngineDone("error", "Claude Code ended without a result.", "internal")

    @staticmethod
    def _stream_event(event: dict[str, Any], message_id: str) -> list[EngineEvent | str]:
        """Map one raw API stream event; a ``str`` item is a new message id."""
        kind = event.get("type")
        if kind == "message_start":
            return [str(event.get("message", {}).get("id") or message_id)]
        if kind == "content_block_start":
            block_type = event.get("content_block", {}).get("type")
            phase = {"thinking": "thinking", "text": "writing", "tool_use": "tool"}.get(block_type)
            return [PhaseEvent(phase)] if phase else []  # type: ignore[arg-type]
        if kind == "content_block_delta":
            delta = event.get("delta", {})
            seg = f"{message_id}:{event.get('index', 0)}"
            if delta.get("type") == "text_delta":
                return [TextDelta(seg, delta.get("text", ""))]
            if delta.get("type") == "thinking_delta":
                return [ThinkingDelta(seg, delta.get("thinking", ""))]
        return []

    @staticmethod
    def _tool_result(
        block: Any, tool_uses: dict[str, tuple[str, Any]], bridge: _ToolBridge
    ) -> tuple[Block, ToolEnd]:
        name, tool_input = tool_uses.get(block.tool_use_id, ("", None))
        out = bridge.outputs.get(_output_key(name, tool_input))
        is_error = bool(block.is_error)
        if out is None:
            # Not one of our handlers' results (e.g. a denied tool).
            text = _result_text(block.content)
            content: list[Block] = [text_block(text)]
            end = ToolEnd(block.tool_use_id, is_error, text[:120])
        else:
            content = [text_block(out.text)]
            content.extend(image_ref_block(i.image_id, i.mime, i.max_size) for i in out.images)
            end = ToolEnd(block.tool_use_id, is_error, out.summary, [i.image_id for i in out.images])
        return tool_result_block(block.tool_use_id, content, is_error), end

    def _done(self, result: Any) -> EngineDone:
        usage = dict(result.usage or {})
        if result.total_cost_usd is not None:
            usage["cost_usd"] = result.total_cost_usd
        if self._interrupting or result.terminal_reason in ("aborted_streaming", "aborted_tools"):
            return EngineDone("interrupted", usage=usage, stop_reason=result.stop_reason)
        if result.is_error:
            detail = " ".join(str(x) for x in (result.result, result.errors) if x) or result.subtype
            if result.subtype == "error_max_turns":
                return EngineDone("error", "Stopped after too many tool calls.", "limit", usage=usage)
            kind: ErrorKind = (
                "auth" if _is_auth_failure(detail, result.api_error_status) else "internal"
            )
            return EngineDone("error", detail, kind, usage=usage, stop_reason=result.stop_reason)
        return EngineDone("done", usage=usage, stop_reason=result.stop_reason)

    def _failure(self, message: str) -> EngineDone:
        detail = f"{message} {' '.join(self._stderr[-3:])}".strip()
        kind: ErrorKind = "auth" if _is_auth_failure(detail) else "internal"
        return EngineDone("error", message, kind)


def _prompt_text(req: TurnRequest) -> str:
    """The new user message as CLI prompt text (context first, as engine A
    renders it). Earlier turns come from the resumed SDK session."""
    last = req.history[-1]
    parts: list[str] = []
    for b in last.content:
        if b.get("type") == "context":
            parts.append(f"<screen_context>\n{b['text']}\n</screen_context>")
        elif b.get("type") == "text":
            parts.append(b["text"])
    return "\n\n".join(parts)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


async def probe_oauth_token(token: str) -> tuple[ProbeResult, str | None]:
    """Run a one-shot, tool-less CLI turn with ``token``.

    Timeouts, a missing CLI and streams without a result say nothing about
    the token: ``inconclusive``, never ``auth_failed``.
    """
    try:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            CLINotFoundError,
            PermissionResultDeny,
            ResultMessage,
            query,
        )
    except ImportError as e:
        return "inconclusive", f"Claude Agent SDK unavailable: {e}"

    async def deny(_name: str, _input: dict[str, Any], _ctx: Any) -> Any:
        return PermissionResultDeny(message="no tools during validation")

    for d in (claude_home(), _workspace()):
        d.mkdir(parents=True, exist_ok=True)
    options = ClaudeAgentOptions(
        tools=[],
        can_use_tool=deny,
        setting_sources=[],
        strict_mcp_config=True,
        max_turns=1,
        system_prompt="Reply with the single word: ok",
        cwd=str(_workspace()),
        env=engine_env(token),
    )
    agen = query(prompt="ping", options=options)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            async for message in agen:
                if isinstance(message, ResultMessage):
                    if not message.is_error:
                        return "ok", None
                    detail = str(message.result or message.subtype)
                    if _is_auth_failure(detail, message.api_error_status):
                        return "auth_failed", detail
                    return "inconclusive", detail
    except TimeoutError:
        return "inconclusive", f"no answer within {PROBE_TIMEOUT_S:.0f}s"
    except CLINotFoundError as e:
        return "inconclusive", f"Claude Code is not available: {e}"
    except Exception as e:
        if _is_auth_failure(str(e)):
            return "auth_failed", str(e)
        return "inconclusive", str(e)
    finally:
        try:
            await asyncio.wait_for(agen.aclose(), 10)
        except Exception:
            logger.debug("probe stream did not close cleanly", exc_info=True)
    return "inconclusive", "Claude Code ended without a result"
