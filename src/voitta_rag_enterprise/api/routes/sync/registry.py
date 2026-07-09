"""Per-source-type handler registry for the sync routes.

Each source module (github.py, google_drive.py, …) registers ONE handler
per ``source_type`` it owns; the source-agnostic core endpoints (GET /
PUT / trigger) dispatch through this table instead of if/elif chains.
Adding a connector = new module + one ``register()`` call — core.py does
not change.

The registry deliberately knows nothing about FastAPI or pydantic: it is
plain callables over ``FolderSyncSource`` rows, so it imports nothing
from its siblings and can never participate in a cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ....db.models import FolderSyncSource


@dataclass(frozen=True)
class SourceHandler:
    """Everything core.py needs to serve one ``source_type``.

    ``family`` groups types that share credential columns — sharepoint and
    teams both live on ``ms_*``, so switching between them must NOT wipe
    those columns. Every other type is its own family.

    ``apply`` is None for types that are not configurable through the PUT
    envelope (google_drive_local rows are created by their own connect
    endpoint). ``build_out``'s result lands on ``SyncSourceOut.<out_field>``.
    ``trigger_check`` runs before a sync job is enqueued and raises
    ``HTTPException`` when the row isn't ready (nothing picked yet, backing
    mount gone, …).
    """

    source_type: str
    out_field: str
    family: str = ""
    in_field: str = ""
    apply: Callable[..., FolderSyncSource] | None = None
    build_out: Callable[[FolderSyncSource], Any] | None = None
    clear: Callable[[FolderSyncSource], None] | None = None
    trigger_check: Callable[[FolderSyncSource], None] | None = None

    def __post_init__(self) -> None:
        if not self.family:
            object.__setattr__(self, "family", self.source_type)
        if not self.in_field:
            object.__setattr__(self, "in_field", self.source_type)


HANDLERS: dict[str, SourceHandler] = {}


def register(handler: SourceHandler) -> None:
    HANDLERS[handler.source_type] = handler


def clear_other_sources(src: FolderSyncSource, new_type: str) -> None:
    """Wipe every OTHER family's columns when a row switches source type.

    Keeps the new type's own family intact (sharepoint↔teams switches keep
    the shared ``ms_*`` credentials). Clear functions shared by several
    handlers (the microsoft pair) run once.
    """
    new_family = HANDLERS[new_type].family
    seen: set[int] = set()
    for h in HANDLERS.values():
        if h.family == new_family or h.clear is None or id(h.clear) in seen:
            continue
        seen.add(id(h.clear))
        h.clear(src)
