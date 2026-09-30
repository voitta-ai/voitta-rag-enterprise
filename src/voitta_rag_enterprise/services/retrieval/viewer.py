"""Who is reading, and which folders count as "theirs" for this caller.

Every retrieval function takes a :class:`Viewer` explicitly instead of
reading ambient request state. The two callers resolve identity very
differently — the MCP server from a bearer-token ContextVar, the in-app
assistant from the WebSocket session (impersonation-aware) — and neither
should reach into the other's plumbing.

``scope`` selects the folder set search runs over:

* ``"mcp_active"`` — visible folders minus the viewer's per-folder MCP
  opt-outs (``folder_user_settings.active = 0``). What external MCP clients
  get; the opt-out exists to keep folders out of *their* LLM context.
* ``"visible"`` — every folder the viewer can see. What the in-app
  assistant gets: the opt-out is an MCP setting, not a statement that the
  user can't read the folder.

``user_id is None`` means unrestricted and is only ever constructed in
single-user mode (the sole identity owns everything) — or by the MCP
server's legacy in-process path, see ``mcp_server._viewer``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ...db.models import File
from ..acl import mcp_visible_folder_ids, user_can_see_folder, visible_folder_ids

FolderScope = Literal["mcp_active", "visible"]


@dataclass(frozen=True)
class Viewer:
    user_id: int | None
    scope: FolderScope = "mcp_active"

    @property
    def unrestricted(self) -> bool:
        return self.user_id is None

    def searchable_folder_ids(self, s: Session) -> set[int] | None:
        """Folders search may touch; ``None`` = no filter (unrestricted)."""
        if self.user_id is None:
            return None
        if self.scope == "visible":
            return set(visible_folder_ids(s, self.user_id))
        return set(mcp_visible_folder_ids(s, self.user_id))

    def visible_folder_ids(self, s: Session) -> set[int] | None:
        """Folders the viewer can read at all; ``None`` = unrestricted."""
        if self.user_id is None:
            return None
        return set(visible_folder_ids(s, self.user_id))

    def search_filter(self, s: Session, requested: list[int] | None) -> list[int] | None:
        """The Qdrant ``folder_ids`` filter for a search.

        Intersects a caller-supplied list with the searchable set so a
        request can never widen its own reach. An empty result becomes
        ``[-1]`` — an impossible id — so Qdrant takes its cheap no-match
        path while the filter keeps its list type.
        """
        allowed = self.searchable_folder_ids(s)
        if allowed is None:
            return requested
        if not allowed:
            return [-1]
        if requested is None:
            return sorted(allowed)
        return [fid for fid in requested if fid in allowed] or [-1]

    def can_see_folder(self, s: Session, folder_id: int) -> bool:
        return self.user_id is None or user_can_see_folder(s, folder_id, self.user_id)

    def require_file(self, s: Session, file_id: int) -> File:
        """Load a file the viewer may read, or raise ``ValueError``.

        The single seam every id-taking retrieval routes through — without
        it a caller could read any file/chunk/image/asset by enumerating
        integer ids. The message is identical for a missing row and an
        ACL-hidden one so foreign ids aren't probeable.
        """
        f = s.get(File, file_id)
        if f is None or not self.can_see_folder(s, f.folder_id):
            raise ValueError(f"File {file_id} not found")
        return f
