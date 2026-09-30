"""Engine A — the Anthropic Messages API with our own streaming tool loop.

Why a hand-written loop rather than the SDK tool runner: every model
message and every batch of tool results must be persisted the moment it is
complete (append-only transcript), tool calls run off the event loop with
the viewer's ACL, images are stored as references and re-hydrated on
replay, and a user's Stop must land between two safe points. Those are
per-round hooks the loop gives us directly.

Request shape, per round:

* ``thinking: adaptive`` with ``display: "summarized"`` (streamed to the
  user as the model's reasoning) and ``output_config.effort``;
* prompt caching: an explicit breakpoint on the fixed system prompt (which
  also covers the tool list rendered before it) plus top-level automatic
  caching of the growing conversation prefix;
* server-side refusal fallbacks (``fallbacks: "default"``) for the models
  whose safety classifiers can decline a benign request — the API reruns
  the request on its recommended fallback model instead of returning the
  refusal.

History is replayed verbatim from the transcript (thinking blocks and all):
thinking continuity requires exact, append-only history.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

from ..credentials import ProbeResult
from ..protocol import (
    EngineDone,
    EngineEvent,
    Persist,
    PhaseEvent,
    TextDelta,
    TextFinal,
    ThinkingDelta,
    ToolEnd,
    ToolStart,
)
from ..store import StoredMessage
from ..tools import ToolContext, ToolOutput, ToolSpec, error_output, replay_image
from ..transcript import Block, image_ref_block, text_block, tool_result_block
from .base import TurnRequest

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 64_000
# Models whose classifiers may decline; they get server-side fallbacks.
FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Block types the API emits that we keep and replay. Anything else (the
# ``fallback`` audit marker, future block types) is not echoed back.
_REPLAYED_TYPES = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})
_UNAVAILABLE_IMAGE = "[image no longer available]"


def render_tools(tools: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema()}
        for t in tools
    ]


def _replay_content(block: Block, ctx: ToolContext) -> list[dict[str, Any]]:
    """API content for one stored tool_result content item."""
    if block.get("type") == "image_ref":
        image = replay_image(ctx, int(block["image_id"]), int(block["max_size"]))
        if image is None:
            return [{"type": "text", "text": _UNAVAILABLE_IMAGE}]
        return [{
            "type": "image",
            "source": {"type": "base64", "media_type": image.mime, "data": image.data_base64},
        }]
    return [{"type": "text", "text": str(block.get("text", ""))}]


def build_api_messages(history: list[StoredMessage], ctx: ToolContext) -> list[dict[str, Any]]:
    """Render the stored transcript as Messages API ``messages``.

    Deterministic: the same transcript always renders to the same bytes
    (images are re-read at their stored size), which is what keeps the
    prompt-cache prefix warm across turns. Consecutive user-role messages
    (e.g. after a turn that failed before the model answered) are legal —
    the API joins them.
    """
    out: list[dict[str, Any]] = []
    for m in history:
        if m.role == "assistant":
            out.append({"role": "assistant", "content": m.content})
        elif m.role == "tool":
            results = []
            for b in m.content:
                content: list[dict[str, Any]] = []
                for item in b.get("content", []):
                    content.extend(_replay_content(item, ctx))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": b["tool_use_id"],
                    "is_error": bool(b.get("is_error")),
                    "content": content,
                })
            out.append({"role": "user", "content": results})
        else:
            content = []
            for b in m.content:
                if b.get("type") == "context":
                    content.append({
                        "type": "text",
                        "text": f"<screen_context>\n{b['text']}\n</screen_context>",
                    })
                elif b.get("type") == "text":
                    content.append({"type": "text", "text": b["text"]})
            out.append({"role": "user", "content": content})
    return out


def normalize_assistant_blocks(blocks: list[dict[str, Any]]) -> list[Block]:
    """Keep what must be replayed; apply the fallback echo rule.

    After a mid-output fallback, only ``text`` blocks from before the last
    ``fallback`` boundary may be echoed back; everything after it echoes
    normally. The ``fallback`` marker itself is an audit block and dropped.
    """
    last_fallback = max(
        (i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1
    )
    kept: list[Block] = []
    for i, b in enumerate(blocks):
        kind = b.get("type")
        if kind not in _REPLAYED_TYPES:
            continue
        if i < last_fallback and kind != "text":
            continue
        kept.append(b)
    return kept


def _tool_result_blocks(tool_use_id: str, out: ToolOutput) -> tuple[dict[str, Any], Block]:
    """(API tool_result, stored tool_result) for one tool output."""
    api_content: list[dict[str, Any]] = [{"type": "text", "text": out.text}]
    stored_content: list[Block] = [text_block(out.text)]
    for img in out.images:
        api_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img.mime, "data": img.data_base64},
        })
        stored_content.append(image_ref_block(img.image_id, img.mime, img.max_size))
    api = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": out.is_error,
        "content": api_content,
    }
    return api, tool_result_block(tool_use_id, stored_content, out.is_error)


def _add_usage(total: dict[str, int], usage: Any) -> None:
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        total[key] = total.get(key, 0) + int(getattr(usage, key, 0) or 0)


class _Final:
    """Internal: the round's complete message, handed from stream to loop."""

    def __init__(self, message: Any) -> None:
        self.message = message


class AnthropicApiEngine:
    """``client_factory`` builds the SDK client from ``api_key`` (injectable
    for tests); defaults to ``anthropic.AsyncAnthropic``."""

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory
        self._interrupted = asyncio.Event()

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def run(self, req: TurnRequest) -> AsyncGenerator[EngineEvent, None]:
        import anthropic

        factory = self._client_factory or anthropic.AsyncAnthropic
        client = factory(api_key=req.credential.secret, max_retries=2)
        usage: dict[str, int] = {}
        try:
            messages = await asyncio.to_thread(build_api_messages, req.history, req.tool_context)
            tools = render_tools(req.tools)
            for _round in range(req.max_tool_rounds):
                message = None
                async for event in self._stream_round(client, req, messages, tools):
                    if isinstance(event, _Final):
                        message = event.message
                    else:
                        yield event
                if message is None:  # interrupted mid-stream
                    yield EngineDone("interrupted", usage=usage)
                    return
                _add_usage(usage, message.usage)
                blocks = normalize_assistant_blocks(
                    [b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in message.content]
                )
                if message.stop_reason == "refusal":
                    # Partial output of a declined request is discarded, not
                    # persisted: it must not become replayed history.
                    yield EngineDone(
                        "error",
                        "The model declined this request.",
                        "refusal",
                        usage=usage,
                        stop_reason="refusal",
                    )
                    return
                if not blocks:
                    yield EngineDone("done", usage=usage, stop_reason=message.stop_reason)
                    return
                yield Persist(
                    "assistant",
                    blocks,
                    usage=message.usage.model_dump(mode="json", exclude_none=True),
                    stop_reason=message.stop_reason,
                )
                messages.append({"role": "assistant", "content": blocks})
                tool_uses = [b for b in blocks if b["type"] == "tool_use"]
                if message.stop_reason == "pause_turn":
                    continue
                if not tool_uses:
                    yield EngineDone("done", usage=usage, stop_reason=message.stop_reason)
                    return
                if message.stop_reason == "max_tokens":
                    # A tool input cut off by the token limit parses as a
                    # partial object; never run it. The TurnRunner seals the
                    # dangling tool_use so the transcript stays replayable.
                    yield EngineDone(
                        "error", "The response hit the output limit.", "limit",
                        usage=usage, stop_reason="max_tokens",
                    )
                    return
                if self._interrupted.is_set():
                    yield EngineDone("interrupted", usage=usage)
                    return
                results_api, results_stored = [], []
                async for event in self._run_tools(req, tool_uses, results_api, results_stored):
                    yield event
                yield Persist("tool", results_stored)
                messages.append({"role": "user", "content": results_api})
                if self._interrupted.is_set():
                    yield EngineDone("interrupted", usage=usage)
                    return
            yield EngineDone(
                "error",
                f"Stopped after {req.max_tool_rounds} rounds of tool calls.",
                "limit",
                usage=usage,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            yield EngineDone("error", _api_message(e), "auth", usage=usage)
        except anthropic.RateLimitError as e:
            yield EngineDone("error", _api_message(e), "limit", usage=usage)
        except anthropic.APIConnectionError as e:
            yield EngineDone("error", f"Could not reach the Anthropic API: {e}", "unavailable", usage=usage)
        except anthropic.APIStatusError as e:
            yield EngineDone("error", _api_message(e), "internal", usage=usage)
        finally:
            await client.close()

    async def _stream_round(
        self,
        client: Any,
        req: TurnRequest,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[EngineEvent | _Final]:
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": [{
                "type": "text",
                "text": req.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "tools": tools,
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": req.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if req.model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        yield PhaseEvent("thinking")
        message_id = "m"
        async with client.beta.messages.stream(**params) as stream:
            async for event in stream:
                if self._interrupted.is_set():
                    return
                kind = event.type
                if kind == "message_start":
                    message_id = event.message.id
                elif kind == "content_block_start":
                    block_type = event.content_block.type
                    if block_type == "thinking":
                        yield PhaseEvent("thinking")
                    elif block_type == "text":
                        yield PhaseEvent("writing")
                    elif block_type == "tool_use":
                        yield PhaseEvent("tool")
                elif kind == "content_block_delta":
                    seg = f"{message_id}:{event.index}"
                    if event.delta.type == "thinking_delta":
                        yield ThinkingDelta(seg, event.delta.thinking)
                    elif event.delta.type == "text_delta":
                        yield TextDelta(seg, event.delta.text)
            message = await stream.get_final_message()
        for index, block in enumerate(message.content):
            if block.type == "text":
                yield TextFinal(f"{message.id}:{index}", block.text)
        yield _Final(message)

    async def _run_tools(
        self,
        req: TurnRequest,
        tool_uses: list[Block],
        results_api: list[dict[str, Any]],
        results_stored: list[Block],
    ) -> AsyncIterator[EngineEvent]:
        """Run one round's tool calls concurrently, in call order for results."""
        by_name = {t.name: t for t in req.tools}
        for tu in tool_uses:
            yield ToolStart(tu["id"], tu["name"], dict(tu.get("input") or {}))

        async def _one(tu: Block) -> ToolOutput:
            spec = by_name.get(tu["name"])
            if spec is None:
                return error_output(f"unknown tool {tu['name']!r}")
            return await asyncio.to_thread(spec.run, req.tool_context, tu.get("input"))

        outputs = await asyncio.gather(*(_one(tu) for tu in tool_uses))
        for tu, out in zip(tool_uses, outputs, strict=True):
            api, stored = _tool_result_blocks(tu["id"], out)
            results_api.append(api)
            results_stored.append(stored)
            yield ToolEnd(tu["id"], out.is_error, out.summary, [i.image_id for i in out.images])


def _api_message(e: Any) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(e, "message", None) or e)


async def probe_api_key(secret: str) -> tuple[ProbeResult, str | None]:
    """Cheap authenticated call (list one model) to verify an API key."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=secret, max_retries=0, timeout=20.0)
    try:
        await client.models.list(limit=1)
        return "ok", None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        return "auth_failed", _api_message(e)
    except anthropic.APIError as e:
        return "inconclusive", _api_message(e)
    finally:
        await client.close()
