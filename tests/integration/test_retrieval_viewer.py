"""``services.retrieval.Viewer`` folder scopes.

``scope="mcp_active"`` (external MCP clients) honours the user's per-folder
MCP opt-out; ``scope="visible"`` (the in-app assistant) searches every
folder the user can see. Both are bounded by folder visibility: another
tenant's folder is unreachable in either scope, including through an
explicit ``folder_ids`` request.
"""

from __future__ import annotations

from pathlib import Path

from tests.integration.seeding import seed_owned
from voitta_rag_enterprise.db.database import session_scope
from voitta_rag_enterprise.services import retrieval
from voitta_rag_enterprise.services.acl import get_or_create_user, set_folder_active
from voitta_rag_enterprise.services.retrieval import Viewer


def _folders_hit(hits) -> set[int]:
    return {h.folder_id for h in hits if h.kind == "chunk"}


def test_scopes_differ_only_on_mcp_opt_out(env: None, tmp_path: Path) -> None:
    kept = seed_owned(tmp_path / "kept", "alice@x", {"k.md": "zebra quartz kept"})
    muted = seed_owned(tmp_path / "muted", "alice@x", {"m.md": "zebra quartz muted"})
    foreign = seed_owned(tmp_path / "bob", "bob@x", {"b.md": "zebra quartz foreign"})
    with session_scope() as s:
        alice = get_or_create_user(s, "alice@x").id
        set_folder_active(s, muted["folder_id"], alice, False)

    mcp = Viewer(user_id=alice, scope="mcp_active")
    assistant = Viewer(user_id=alice, scope="visible")

    assert _folders_hit(retrieval.search(mcp, "zebra quartz")) == {kept["folder_id"]}
    assert _folders_hit(retrieval.search(assistant, "zebra quartz")) == {
        kept["folder_id"],
        muted["folder_id"],
    }

    # Asking for a foreign folder explicitly never widens reach.
    for viewer in (mcp, assistant):
        hits = retrieval.search(viewer, "zebra quartz", folder_ids=[foreign["folder_id"]])
        assert _folders_hit(hits) == set()

    roots = {f.id: f.active for f in retrieval.list_indexed_folders(assistant)}
    assert roots == {kept["folder_id"]: True, muted["folder_id"]: True}
    roots = {f.id: f.active for f in retrieval.list_indexed_folders(mcp)}
    assert roots == {kept["folder_id"]: True, muted["folder_id"]: False}


def test_unrestricted_viewer_sees_everything(env: None, tmp_path: Path) -> None:
    a = seed_owned(tmp_path / "a", "alice@x", {"a.md": "walrus alpha"})
    b = seed_owned(tmp_path / "b", "bob@x", {"b.md": "walrus beta"})
    hits = retrieval.search(Viewer(user_id=None), "walrus")
    assert _folders_hit(hits) == {a["folder_id"], b["folder_id"]}
