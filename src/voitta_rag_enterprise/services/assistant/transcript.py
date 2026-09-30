"""The stored transcript format — engine-neutral content blocks.

One ``assistant_messages`` row is one model-facing message; its
``content_json`` is a list of these blocks:

``user`` rows
    ``{"type": "text", "text"}`` — what the person typed.
    ``{"type": "context", "text"}`` — screen context captured with the
    question (selected folder etc.). Shown to the model, hidden in the UI.

``assistant`` rows
    ``{"type": "text", "text"}``
    ``{"type": "thinking", "thinking", "signature"}`` /
    ``{"type": "redacted_thinking", "data"}`` — replayed verbatim to the
    API engine (thinking continuity requires exact, append-only history);
    the UI shows the summarized ``thinking`` text.
    ``{"type": "tool_use", "id", "name", "input"}``

``notice`` rows (shown to people, NEVER sent to a model)
    ``{"type": "notice", "kind": "error" | "interrupted", "text"}`` — how a
    turn ended when it did not end normally, so a reloaded conversation
    shows why a question has no (complete) answer.

``tool`` rows (sent to the model in the user role)
    ``{"type": "tool_result", "tool_use_id", "is_error", "content": [...]}``
    whose content holds ``text`` blocks and ``{"type": "image_ref",
    "image_id", "mime", "max_size"}`` blocks. Images are referenced, never
    inlined: the bytes are re-read at the same ``max_size`` (deterministic,
    so the replayed prompt prefix stays byte-identical and cacheable) and
    re-checked against the viewer's ACL when the history is replayed — the
    DB stays small and a revoked folder's images stop flowing.
"""

from __future__ import annotations

from typing import Any, Literal

Role = Literal["user", "assistant", "tool", "notice"]
Block = dict[str, Any]


def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


def context_block(text: str) -> Block:
    return {"type": "context", "text": text}


def tool_use_block(tool_id: str, name: str, tool_input: dict[str, Any]) -> Block:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def image_ref_block(image_id: int, mime: str, max_size: int) -> Block:
    return {"type": "image_ref", "image_id": image_id, "mime": mime, "max_size": max_size}


def tool_result_block(tool_use_id: str, content: list[Block], is_error: bool) -> Block:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": is_error,
        "content": content,
    }


def notice_block(kind: str, text: str) -> Block:
    return {"type": "notice", "kind": kind, "text": text}


def plain_text(blocks: list[Block]) -> str:
    """Concatenated ``text`` blocks (titles, previews)."""
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text").strip()
