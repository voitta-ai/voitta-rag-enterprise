"""Shared plumbing for the admin route package.

Import-layer 0: nothing here imports a sibling module at module level, so
every endpoint module can depend on it without cycles. ``publish_admin_state``
lazy-imports ``build_admin_state`` from ``state`` inside its body to keep that
edge deferred.
"""

from __future__ import annotations

from fastapi import APIRouter

from ....db.database import session_scope
from ....services import events

router = APIRouter(prefix="/admin", tags=["admin"])


def publish_admin_state() -> None:
    """Push the full admin state to every admin WS connection.

    Low-volume (admin mutations are rare), so re-sending the whole state on each
    change keeps the client logic to a single replace with no delta merging.
    """
    from .state import build_admin_state

    with session_scope() as db:
        state = build_admin_state(db)
    events.publish("admin", {"type": "admin.snapshot", "state": state})
