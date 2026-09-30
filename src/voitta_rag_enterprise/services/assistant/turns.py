"""TurnRunner — runs assistant turns independently of any one socket.

A turn is an asyncio task owned by the runner, not by the WebSocket that
asked for it: closing the tab does not lose the answer. Every complete
message is persisted as it arrives, and live events are fanned out to
whichever sockets are watching the conversation at that moment (a
reconnecting client reloads the transcript over REST and re-watches).

Guarantees:

* one active turn per conversation; at most
  ``VOITTA_ASSISTANT_MAX_CONCURRENT_TURNS`` across the process; a
  wall-clock bound per turn;
* Stop is cooperative (engine ``interrupt``) with a hard cancel after a
  grace period;
* the transcript is always replayable: a turn that ends between a model's
  tool calls and their results (stop, error, timeout) gets those calls
  sealed with error results;
* DB work never runs on the event loop (``asyncio.to_thread``).

State is in-process: the deployment runs a single uvicorn worker (see
Dockerfile), which this module relies on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ...config import get_settings
from ...db.database import session_scope
from ..retrieval import Viewer
from . import credentials, store
from .catalog import EFFORTS, ENGINE_IDS, MODEL_IDS, EngineId
from .engines.base import Engine, TurnRequest
from .identity import AssistantIdentity, ConversationView
from .policy import load_policy
from .prompts import SYSTEM_PROMPT, screen_context
from .protocol import (
    ConversationUpdated,
    EngineDone,
    ErrorKind,
    Persist,
    SdkSession,
    TurnEnd,
    TurnStart,
)
from .tools import TOOLS, ToolContext
from .transcript import Block, context_block, text_block, tool_result_block

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 32_000
TITLE_CHARS = 60
STOP_GRACE_S = 5.0

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]
EngineFactory = Callable[[EngineId], Engine]


class TurnRejected(Exception):
    def __init__(self, message: str, kind: ErrorKind = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class AskRequest:
    text: str
    conversation_id: int | None = None
    # New conversations only: whose list it joins, and its pinned engine/model.
    view: ConversationView = "mine"
    engine: EngineId | None = None
    model: str | None = None
    effort: str | None = None
    ui_context: dict[str, Any] | None = None


@dataclass
class _Prepared:
    conversation: dict[str, Any]
    # New conversation or new title: announce it to watchers.
    announce: bool
    engine_id: EngineId
    request: TurnRequest


@dataclass
class _ActiveTurn:
    conversation_id: int
    owner_user_id: int
    engine: Engine
    task: asyncio.Task[None] | None = None
    subscribers: set[Subscriber] = field(default_factory=set)


def default_engine_factory(engine_id: EngineId) -> Engine:
    if engine_id == "anthropic_api":
        from .engines.anthropic_api import AnthropicApiEngine

        return AnthropicApiEngine()
    from .engines.claude_subscription import ClaudeSubscriptionEngine

    return ClaudeSubscriptionEngine()


class TurnRunner:
    def __init__(
        self,
        engine_factory: EngineFactory = default_engine_factory,
        *,
        max_concurrent: int,
        max_tool_rounds: int,
        timeout_s: float,
    ) -> None:
        self._factory = engine_factory
        self._max_concurrent = max_concurrent
        self._max_tool_rounds = max_tool_rounds
        self._timeout_s = timeout_s
        self._active: dict[int, _ActiveTurn] = {}
        # Watchers of conversations with no running turn (a turn started
        # later picks them up); keyed by conversation id.
        self._watchers: dict[int, set[Subscriber]] = {}
        self._lock = asyncio.Lock()

    # --- watching -------------------------------------------------------

    def watch(self, conversation_id: int, subscriber: Subscriber) -> bool:
        """Deliver this conversation's live events to ``subscriber``.

        The caller has already checked access. Returns whether a turn is
        running right now.
        """
        self.unwatch(subscriber)  # one conversation in focus per subscriber
        self._watchers.setdefault(conversation_id, set()).add(subscriber)
        turn = self._active.get(conversation_id)
        if turn is not None:
            turn.subscribers.add(subscriber)
            return True
        return False

    def unwatch(self, subscriber: Subscriber) -> None:
        for subs in self._watchers.values():
            subs.discard(subscriber)
        for turn in self._active.values():
            turn.subscribers.discard(subscriber)
        for cid in [c for c, s in self._watchers.items() if not s]:
            del self._watchers[cid]

    def is_running(self, conversation_id: int) -> bool:
        return conversation_id in self._active

    # --- asking ---------------------------------------------------------

    async def ask(
        self, ident: AssistantIdentity, req: AskRequest, subscriber: Subscriber
    ) -> int:
        """Validate, persist the user message and start the turn; the asking
        ``subscriber`` then watches the conversation.

        Returns the conversation id (new conversations are created here).
        Raises :class:`TurnRejected` when the turn can't start.
        """
        text = req.text.strip()
        if not text:
            raise TurnRejected("The message is empty.")
        if len(text) > MAX_PROMPT_CHARS:
            raise TurnRejected(f"The message is longer than {MAX_PROMPT_CHARS} characters.")
        async with self._lock:
            if req.conversation_id is not None and req.conversation_id in self._active:
                raise TurnRejected("A reply is still being written in this conversation.")
            if len(self._active) >= self._max_concurrent:
                raise TurnRejected(
                    "The assistant is busy with other conversations. Try again in a moment.",
                    "limit",
                )
            prepared = await asyncio.to_thread(self._prepare, ident, req, text)
            cid = int(prepared.conversation["id"])
            turn = _ActiveTurn(
                conversation_id=cid,
                owner_user_id=prepared.conversation["owner_user_id"],
                # Created now, not in the task: a Stop that arrives before
                # the task first runs must still reach the engine.
                engine=self._factory(prepared.engine_id),
            )
            self.watch(cid, subscriber)
            turn.subscribers |= self._watchers.get(cid, set())
            self._active[cid] = turn
            turn.task = asyncio.create_task(
                self._drive(turn, prepared), name=f"assistant-turn-{cid}"
            )
        if prepared.announce:
            await self._broadcast(turn, ConversationUpdated(prepared.conversation).to_wire())
        await self._broadcast(turn, TurnStart(cid).to_wire())
        return cid

    def _prepare(self, ident: AssistantIdentity, req: AskRequest, text: str) -> _Prepared:
        policy = load_policy()
        if not policy.enabled:
            raise TurnRejected("The assistant is turned off for this deployment.", "unavailable")
        effort = req.effort if req.effort in EFFORTS else policy.default_effort
        with session_scope() as db:
            announce = False
            if req.conversation_id is None:
                engine_id = req.engine if req.engine in ENGINE_IDS else self._default_engine(db, ident)
                model = req.model if req.model in MODEL_IDS else policy.default_model
                conv = store.create_conversation(
                    db,
                    owner_user_id=ident.owner_for(req.view),
                    created_by_user_id=ident.real.id,
                    engine=engine_id,
                    model=model,
                )
                announce = True
            else:
                found = store.get_conversation(db, req.conversation_id)
                if found is None or not ident.can_access_owner(found.owner_user_id):
                    raise TurnRejected("Conversation not found.")
                conv = found
                engine_id = conv.engine  # type: ignore[assignment]
            if engine_id == "claude_subscription" and not ident.can_use_subscription:
                raise TurnRejected(
                    "This conversation runs on the Claude subscription, which only "
                    "super-admins can use.",
                    "auth",
                )
            cred = credentials.resolve(db, ident, engine_id)
            if cred is None:
                raise TurnRejected(
                    "No credential is configured for this engine. Add one in "
                    "Settings → Assistant.",
                    "auth",
                )
            content: list[Block] = []
            context = screen_context(req.ui_context)
            if context:
                content.append(context_block(context))
            content.append(text_block(text))
            store.append_message(
                db,
                conv,
                role="user",
                content=content,
                author_user_id=ident.real.id,
                acting_user_id=ident.effective.id,
            )
            if not conv.title:
                first_line = text.splitlines()[0]
                store.update_conversation(db, conv, title=first_line[:TITLE_CHARS])
                announce = True
            history = store.messages(db, conv.id)
            request = TurnRequest(
                conversation_id=conv.id,
                model=conv.model,
                effort=effort,
                credential=cred,
                system_prompt=SYSTEM_PROMPT,
                history=history,
                tools=TOOLS,
                tool_context=ToolContext(Viewer(user_id=self._viewer_id(ident), scope="visible")),
                max_tool_rounds=self._max_tool_rounds,
                sdk_session_id=conv.sdk_session_id,
            )
            return _Prepared(store.conversation_out(conv), announce, engine_id, request)

    @staticmethod
    def _viewer_id(ident: AssistantIdentity) -> int | None:
        """Tools run as the effective account; unrestricted only in
        single-user mode, where that one identity owns every folder."""
        return None if get_settings().single_user else ident.effective.id

    @staticmethod
    def _default_engine(db: Any, ident: AssistantIdentity) -> EngineId:
        for engine_id in ENGINE_IDS:
            if engine_id == "claude_subscription" and not ident.can_use_subscription:
                continue
            if credentials.resolve(db, ident, engine_id) is not None:
                return engine_id
        return ENGINE_IDS[0]

    # --- running --------------------------------------------------------

    async def _drive(self, turn: _ActiveTurn, prepared: _Prepared) -> None:
        req = prepared.request
        final: EngineDone | None = None
        try:
            events = turn.engine.run(req)
            try:
                async with asyncio.timeout(self._timeout_s):
                    async for event in events:
                        if isinstance(event, Persist):
                            await asyncio.to_thread(self._persist, req.conversation_id, event, req)
                        elif isinstance(event, SdkSession):
                            await asyncio.to_thread(
                                self._set_sdk_session, req.conversation_id, event.session_id
                            )
                        elif isinstance(event, EngineDone):
                            final = event
                        else:
                            await self._broadcast(turn, event.to_wire())
            finally:
                # Close the engine's generator explicitly (timeout / cancel /
                # error): that is what tears down its HTTP stream or its
                # Claude Code subprocess. Bounded so a wedged engine can't
                # hang the turn's cleanup.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(events.aclose(), STOP_GRACE_S)
            if final is None:
                final = EngineDone("error", "The engine ended without a result.", "internal")
        except TimeoutError:
            final = EngineDone("error", "The reply took too long and was stopped.", "limit")
        except asyncio.CancelledError:
            final = EngineDone("interrupted")
        except Exception:
            logger.exception("assistant turn failed (conversation %s)", req.conversation_id)
            final = EngineDone("error", "The assistant hit an internal error.", "internal")
        finally:
            done = final or EngineDone("interrupted")
            try:
                await asyncio.to_thread(self._finish, req, done)
            except Exception:
                logger.exception("assistant turn cleanup failed (conversation %s)", req.conversation_id)
            self._active.pop(req.conversation_id, None)
            await self._broadcast(
                turn,
                TurnEnd(
                    req.conversation_id,
                    done.status,
                    done.error,
                    done.error_kind,
                    done.usage,
                    done.stop_reason,
                ).to_wire(),
            )

    def _persist(self, conversation_id: int, event: Persist, req: TurnRequest) -> None:
        with session_scope() as db:
            conv = store.get_conversation(db, conversation_id)
            if conv is None:  # deleted mid-turn
                return
            store.append_message(
                db,
                conv,
                role=event.role,
                content=event.content,
                acting_user_id=req.tool_context.viewer.user_id,
                usage=event.usage,
                stop_reason=event.stop_reason,
            )

    @staticmethod
    def _set_sdk_session(conversation_id: int, session_id: str) -> None:
        with session_scope() as db:
            conv = store.get_conversation(db, conversation_id)
            if conv is not None:
                store.set_sdk_session(db, conv, session_id)

    @staticmethod
    def _finish(req: TurnRequest, done: EngineDone) -> None:
        with session_scope() as db:
            conv = store.get_conversation(db, req.conversation_id)
            if conv is not None:
                _seal_dangling_tool_uses(db, conv)
            if done.status == "done":
                credentials.record_verification(db, req.credential.credential_id, None)
            elif done.error_kind == "auth":
                credentials.record_verification(db, req.credential.credential_id, done.error)

    # --- stopping -------------------------------------------------------

    async def stop(self, ident: AssistantIdentity, conversation_id: int) -> None:
        turn = self._active.get(conversation_id)
        if turn is None or not ident.can_access_owner(turn.owner_user_id):
            return
        await turn.engine.interrupt()
        task = turn.task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_GRACE_S)
        except TimeoutError:
            # The engine didn't reach a safe point in time: cancel, then wait
            # for the turn's cleanup (sealing, TurnEnd) so a caller that
            # awaited stop() sees the conversation idle.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, STOP_GRACE_S * 2)

    async def shutdown(self) -> None:
        tasks = [t.task for t in self._active.values() if t.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # --- fan-out --------------------------------------------------------

    async def _broadcast(self, turn: _ActiveTurn, frame: dict[str, Any]) -> None:
        for sub in list(turn.subscribers):
            try:
                await sub(frame)
            except Exception:
                # A dead socket: its handler will unwatch on disconnect.
                turn.subscribers.discard(sub)


def _seal_dangling_tool_uses(db: Any, conv: Any) -> None:
    """If the transcript ends in model tool calls without results, append
    error results so the next turn's history is valid."""
    history = store.messages(db, conv.id)
    if not history or history[-1].role != "assistant":
        return
    pending = [b for b in history[-1].content if b.get("type") == "tool_use"]
    if not pending:
        return
    store.append_message(
        db,
        conv,
        role="tool",
        content=[
            tool_result_block(b["id"], [text_block("Not run: the turn ended first.")], True)
            for b in pending
        ],
    )


@lru_cache(maxsize=1)
def get_turn_runner() -> TurnRunner:
    """The process-wide runner (a FastAPI dependency — tests override it)."""
    s = get_settings()
    return TurnRunner(
        max_concurrent=s.assistant_max_concurrent_turns,
        max_tool_rounds=s.assistant_max_tool_rounds,
        timeout_s=s.assistant_turn_timeout_s,
    )


async def shutdown_turn_runner() -> None:
    if get_turn_runner.cache_info().currsize:
        await get_turn_runner().shutdown()
