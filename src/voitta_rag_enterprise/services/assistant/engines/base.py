"""The engine contract.

An engine runs ONE turn: it receives everything it needs in a
:class:`TurnRequest` and yields ``EngineEvent``s (services/assistant/
protocol.py) — wire events to stream to the user, ``Persist`` directives
for every complete transcript message, and exactly one final
``EngineDone``. Engines never touch the database or sockets themselves;
the TurnRunner owns persistence, fan-out, limits and cancellation.

Interruption is cooperative first: ``interrupt()`` asks the engine to stop
at the next safe point and finish with ``EngineDone("interrupted")``. The
TurnRunner cancels the task if that doesn't happen within a grace period.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol

from ..credentials import ResolvedCredential
from ..protocol import EngineEvent
from ..store import StoredMessage
from ..tools import ToolContext, ToolSpec


@dataclass(frozen=True)
class TurnRequest:
    conversation_id: int
    model: str
    effort: str
    credential: ResolvedCredential
    system_prompt: str
    # The whole stored transcript, the new user message included (last).
    history: list[StoredMessage]
    tools: tuple[ToolSpec, ...]
    tool_context: ToolContext
    max_tool_rounds: int
    # Subscription engine: session to resume (None = first turn).
    sdk_session_id: str | None = None


class Engine(Protocol):
    def run(self, req: TurnRequest) -> AsyncGenerator[EngineEvent, None]: ...

    async def interrupt(self) -> None: ...
