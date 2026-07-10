"""Shared plumbing for the admin route package.

Import-layer 0: nothing here imports a sibling module at module level, so
every endpoint module can depend on it without cycles. ``publish_admin_state``
lazy-imports ``build_admin_state`` from ``state`` inside its body to keep that
edge deferred.
"""

from __future__ import annotations

from fastapi import APIRouter

from ....services import events

router = APIRouter(prefix="/admin", tags=["admin"])


def publish_admin_state() -> None:
    """Signal every admin WS connection that admin state changed.

    The admin snapshot is now per-viewer (scoped to each admin's
    administrative domain), so a single broadcast payload can't serve
    everyone. Instead this emits a payload-less ``admin.invalidated``; the WS
    pump rebuilds and sends each admin connection its *own* scoped snapshot
    (see ``api/ws.py``). Admin mutations are rare, so the extra rebuild is
    cheap. Kept synchronous so every (sync) route handler can call it.
    """
    events.publish("admin", {"type": "admin.invalidated"})
