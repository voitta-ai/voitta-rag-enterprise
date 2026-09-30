"""Events a turn produces — the ``/ws/assistant`` wire format, server → client.

Two families:

* **Wire events** (``WireEvent``) are JSON-serialised and broadcast to every
  socket watching the conversation. Each carries ``type`` plus its fields
  (``to_wire``). ``seg`` identifies one streamed text/thinking block so the
  client can append deltas and then replace with the final text.
* **Engine directives** (``Persist``, ``SdkSession``) never reach a socket:
  the TurnRunner consumes them to write the transcript.

Client → server frames are documented on ``api/assistant_ws.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .transcript import Block, Role

PROTOCOL_VERSION = 1

ErrorKind = Literal["auth", "unavailable", "limit", "refusal", "invalid", "internal"]
TurnStatus = Literal["done", "interrupted", "error"]


@dataclass(frozen=True)
class _Wire:
    def to_wire(self) -> dict[str, Any]:
        # ``type`` is a (non-init) dataclass field on every subclass.
        return asdict(self)


@dataclass(frozen=True)
class TurnStart(_Wire):
    conversation_id: int
    running: bool = True
    type: str = field(default="turn_start", init=False)


@dataclass(frozen=True)
class PhaseEvent(_Wire):
    phase: Literal["thinking", "writing", "tool"]
    type: str = field(default="phase", init=False)


@dataclass(frozen=True)
class ThinkingDelta(_Wire):
    seg: str
    text: str
    type: str = field(default="thinking_delta", init=False)


@dataclass(frozen=True)
class TextDelta(_Wire):
    seg: str
    text: str
    type: str = field(default="text_delta", init=False)


@dataclass(frozen=True)
class TextFinal(_Wire):
    seg: str
    text: str
    type: str = field(default="text", init=False)


@dataclass(frozen=True)
class ToolStart(_Wire):
    id: str
    name: str
    input: dict[str, Any]
    type: str = field(default="tool_start", init=False)


@dataclass(frozen=True)
class ToolEnd(_Wire):
    id: str
    is_error: bool
    # Short human summary for the tool card (full result is in the transcript).
    summary: str
    # Images the tool returned, displayed via GET /api/images/{image_id}.
    image_ids: list[int] = field(default_factory=list)
    type: str = field(default="tool_end", init=False)


@dataclass(frozen=True)
class TurnEnd(_Wire):
    conversation_id: int
    status: TurnStatus
    error: str | None = None
    error_kind: ErrorKind | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    type: str = field(default="turn_end", init=False)


@dataclass(frozen=True)
class ConversationUpdated(_Wire):
    conversation: dict[str, Any]
    type: str = field(default="conversation", init=False)


@dataclass(frozen=True)
class ErrorEvent(_Wire):
    message: str
    kind: ErrorKind = "invalid"
    type: str = field(default="error", init=False)


WireEvent = (
    TurnStart | PhaseEvent | ThinkingDelta | TextDelta | TextFinal | ToolStart | ToolEnd
    | TurnEnd | ConversationUpdated | ErrorEvent
)


@dataclass(frozen=True)
class Persist:
    """Append one transcript message (engine → TurnRunner)."""

    role: Role
    content: list[Block]
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class SdkSession:
    """The subscription engine's session id, for resuming the next turn."""

    session_id: str


@dataclass(frozen=True)
class EngineDone:
    """The engine's verdict on the turn (engine → TurnRunner)."""

    status: TurnStatus
    error: str | None = None
    error_kind: ErrorKind | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None


EngineEvent = (
    PhaseEvent | ThinkingDelta | TextDelta | TextFinal | ToolStart | ToolEnd
    | Persist | SdkSession | EngineDone
)
