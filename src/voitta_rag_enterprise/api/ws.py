"""WebSocket endpoint — the single channel for server→client state.

Flow per connection:

1. **Auth** — the handshake is authenticated from the signed session cookie
   (same identity as the REST API). Unauthenticated connections are closed with
   ``4401`` unless single-user mode is on.
2. **Subscribe** — the client's first frame is ``{type:"subscribe", topics}``.
3. **Snapshot** — the server sends the full current state for each subscribed
   topic, scoped to the user's visible folders, then a ``{type:"synced"}``
   sentinel. This is what makes reconnect bulletproof: the client REPLACES its
   stores from the snapshot, so anything missed while offline converges back to
   server truth with no page reload.
4. **Deltas** — the pump drains the coalesced event buffer in batches and
   streams them, filtered per-connection by folder ACL.

After the handshake the channel is server→client only; further client frames
are ignored at the application layer (starlette/uvicorn handle ping/pong).

ACL filtering: each connection caches the set of folders its user can see.
Anything that can change that set bumps ``events.acl_version()``; the pump
notices and recomputes the set (off-thread, so the event loop never blocks on
the DB) before filtering the next batch.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..config import get_settings
from ..db.database import get_session_factory
from ..db.models import User
from ..services import events
from ..services.acl import visible_folder_ids
from ..services.admin_scope import AdminScope, resolve_admin_scope
from .deps import resolve_ws_user
from .snapshot import build_snapshot

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_TOPICS = ("folders", "files", "jobs", "stats", "admin", "keys")

# How long the pump idles between drain checks when nothing is happening.
# The wakeup Event short-circuits this on any actual publish, so a higher
# value just means less CPU when idle.
IDLE_TIMEOUT = 5.0
# Cap per drain — keeps any single send small enough to not stall the socket
# for too long, but large enough that the typical burst leaves in one shot.
MAX_BATCH = 512

# Application-level close code for an unauthenticated connection (4000-4999 is
# the private-use range). The SPA treats this as "stop reconnecting, send the
# user to sign in" rather than the transient-network reconnect path.
WS_CLOSE_UNAUTHENTICATED = 4401


def _resolve_visible(user_id: int) -> set[int]:
    """Compute a user's visible-folder set in a fresh session (off event loop)."""
    factory = get_session_factory()
    db = factory()
    try:
        return set(visible_folder_ids(db, user_id))
    finally:
        db.close()


def _authenticate(ws: WebSocket) -> tuple[int | None, bool, set[int] | None] | None:
    """Resolve ``(user_id, is_admin, visible)`` for the connection.

    Returns ``None`` when the caller is not signed in (multi-user) — the handler
    closes the socket. ``visible is None`` means see-everything; otherwise it's
    the user's visible-folder set.

    Folder visibility for an admin is the **same** as for any other user
    (owned + granted + shared) — admins are not folder-superusers. This mirrors
    ``routes/folders.list_folders``, which filters by ``visible_folder_ids`` for
    everyone in multi-user mode; an empty folder one user creates must not show
    up in another user's tree, admin or not. ``is_admin`` only governs the
    separate ``admin`` topic (the admin console), never folder/file/job
    visibility. ``visible is None`` is reserved for single-user mode, where the
    sole identity owns everything.
    """
    session = ws.session if "session" in ws.scope else None
    factory = get_session_factory()
    db = factory()
    try:
        resolved = resolve_ws_user(session, db)
        if resolved is None:
            return None
        user, is_admin = resolved
        if get_settings().single_user:
            return user.id, is_admin, None
        return user.id, is_admin, set(visible_folder_ids(db, user.id))
    finally:
        db.close()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    auth = _authenticate(ws)
    if auth is None:
        try:
            await ws.send_json({"type": "error", "message": "unauthenticated"})
            await ws.close(code=WS_CLOSE_UNAUTHENTICATED)
        except (WebSocketDisconnect, RuntimeError):
            pass
        return
    user_id, is_admin, visible = auth

    try:
        first = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        return

    if first.get("type") != "subscribe":
        try:
            await ws.send_json(
                {"type": "error", "message": "first message must be subscribe"}
            )
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass
        return

    topics = tuple(
        t for t in (first.get("topics") or VALID_TOPICS) if t in VALID_TOPICS
    )
    if not topics:
        try:
            await ws.send_json({"type": "error", "message": "no valid topics"})
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass
        return

    await ws.send_json({"type": "subscribed", "topics": list(topics)})

    async with events.subscribe(
        topics, user_id=user_id, is_admin=is_admin, visible=visible
    ) as sub:
        # Snapshot AFTER attach: any delta arriving during snapshot build is
        # buffered and applied after, and because the client treats snapshots
        # as replace and deltas as upsert, the race is benign.
        try:
            await _send_snapshot(ws, sub, user_id, is_admin, visible, topics)
        except (WebSocketDisconnect, RuntimeError):
            return
        try:
            await _pump(ws, sub)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return


async def _send_snapshot(
    ws: WebSocket,
    sub: events.Subscription,
    user_id: int | None,
    is_admin: bool,
    visible: set[int] | None,
    topics: tuple[str, ...],
) -> None:
    """Build and send the full state snapshot, then a ``synced`` sentinel."""
    # Resolve the admin domain BEFORE the thread hop — it involves an async
    # Clerk lookup and the sync snapshot builder must not do I/O.
    admin_scope = (
        await _admin_scope_for(user_id) if (is_admin and "admin" in topics) else None
    )
    frames = await asyncio.to_thread(
        _build_snapshot_frames, user_id, is_admin, visible, topics, admin_scope
    )
    for frame in frames:
        await ws.send_text(json.dumps(frame))
    await ws.send_text(json.dumps({"type": "synced"}))


def _build_snapshot_frames(
    user_id: int | None,
    is_admin: bool,
    visible: set[int] | None,
    topics: tuple[str, ...],
    admin_scope: AdminScope | None = None,
) -> list[dict]:
    factory = get_session_factory()
    db = factory()
    try:
        return build_snapshot(
            db,
            user_id=user_id or 0,
            is_admin=is_admin,
            visible=visible,
            topics=topics,
            admin_scope=admin_scope,
        )
    finally:
        db.close()


async def _admin_scope_for(user_id: int | None) -> AdminScope:
    """Resolve a connection's administrative domain (fail closed to empty).

    Subscriptions carry ``user_id`` but not email, so we look the email up
    here. A missing user row yields an empty ``AdminScope`` — never see-all.
    """
    if user_id is None:
        return AdminScope()
    factory = get_session_factory()
    db = factory()
    try:
        row = db.get(User, user_id)
        if row is None:
            return AdminScope()
        # resolve_admin_scope may await Clerk (cached ~45s); holding this
        # short-lived session across the await is fine on this rare path.
        return await resolve_admin_scope(db, row.email)
    finally:
        db.close()


async def _send_admin_frame(ws: WebSocket, sub: events.Subscription) -> None:
    """Rebuild and send this connection's OWN scoped admin snapshot.

    Called when an ``admin.invalidated`` signal arrives: each admin
    connection re-resolves its domain and gets a snapshot scoped to it, so a
    mutation never leaks another admin's out-of-domain view."""
    scope = await _admin_scope_for(sub.user_id)
    frame = await asyncio.to_thread(_build_admin_frame, scope)
    await ws.send_text(json.dumps(frame))


def _build_admin_frame(scope: AdminScope) -> dict:
    from .routes.admin import build_admin_state

    factory = get_session_factory()
    db = factory()
    try:
        return {"type": "admin.snapshot", "state": build_admin_state(db, scope)}
    finally:
        db.close()


async def _refresh_acl_if_stale(sub: events.Subscription) -> set[int] | None:
    """Recompute the connection's visible set if the global ACL version moved.

    Returns the visible set *as it was before* the refresh (so the pump can
    filter the just-drained batch against the union of old and new — see
    ``_pump``). Runs the DB query in a thread so the event loop never blocks.
    Admin / single-user connections (``visible is None``) never filter.
    """
    before = sub.visible
    if sub.visible is None or sub.user_id is None:
        return before
    current = events.acl_version()
    if current == sub.acl_version_seen:
        return before
    sub.visible = await asyncio.to_thread(_resolve_visible, sub.user_id)
    sub.acl_version_seen = current
    return before


def _deliverable(
    event: dict, sub: events.Subscription, allowed: set[int] | None
) -> bool:
    """Per-connection delivery predicate covering all three scoping planes.

    - ``admin.*`` events go only to admin connections.
    - ``keys.*`` events go only to the connection whose user they belong to
      (enforced even for admins — API keys are personal, not folder data).
    - everything else is folder-scoped: delivered when its folder is in
      ``allowed`` (``None`` = admin/single-user sees all), or when it has no
      folder (global events like gc_cas jobs).
    """
    etype = event.get("type", "")
    if etype.startswith("admin."):
        return sub.is_admin
    if etype.startswith("keys."):
        return event.get("user_id") == sub.user_id
    if allowed is None:
        return True
    fid = events._event_folder_id(event)
    return fid is None or fid in allowed


async def _pump(ws: WebSocket, sub: events.Subscription) -> None:
    """Forward coalesced, ACL-filtered events to the client until disconnect.

    Each iteration: wait for a publish (or IDLE_TIMEOUT), refresh the visible
    set if ACL changed, drain the buffer, drop events for folders this user
    can't see, then send what remains as one frame.

    Removal subtlety: deleting a folder makes it *no longer visible*, but the
    client still needs the ``folder.removed`` / ``file.deleted`` event to drop
    it from its store. So we filter against the UNION of the pre- and
    post-refresh visible sets: a folder the user *could* see (and is now gone)
    still passes, while a folder they never could see stays filtered out (no
    leak). Revocation via unshare/ungrant fully propagates on the next
    reconnect snapshot.
    """
    while ws.client_state == WebSocketState.CONNECTED:
        try:
            await sub.wait(timeout=IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        before = await _refresh_acl_if_stale(sub)
        drained = sub.drain(max_events=MAX_BATCH)
        # ``allowed`` is the union of pre/post-refresh visible folders, or None
        # for admin/single-user (no folder filtering). Topic scoping for the
        # admin/keys planes is applied regardless of folder visibility.
        allowed = None if sub.visible is None else sub.visible | (before or set())
        events_out = [e for e in drained if _deliverable(e, sub, allowed)]
        # ``admin.invalidated`` is a rebuild SIGNAL, not a forwardable frame:
        # each admin connection answers it by re-sending its own scoped
        # ``admin.snapshot``. Collapse any number of signals in this drain
        # into one rebuild.
        needs_admin_refresh = (
            sub.is_admin
            and "admin" in sub.topics
            and any(e.get("type") == "admin.invalidated" for e in events_out)
        )
        events_out = [e for e in events_out if e.get("type") != "admin.invalidated"]
        try:
            if events_out:
                payload = (
                    events_out[0]
                    if len(events_out) == 1
                    else {"type": "batch", "events": events_out}
                )
                # send_text + json.dumps so we control framing and avoid
                # starlette's default helper which dumps every event.
                await ws.send_text(json.dumps(payload))
            if needs_admin_refresh:
                await _send_admin_frame(ws, sub)
        except (WebSocketDisconnect, RuntimeError):
            return
