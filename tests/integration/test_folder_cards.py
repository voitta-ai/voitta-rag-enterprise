"""Folder cards: searchable folder/subfolder names + descriptions.

Covers the invariants the feature depends on:

- cards land in the chunks collection and are hit by hybrid search
- rebuild is idempotent (change detection) and diffs adds/removals
- the chunk orphan sweep must NOT eat cards; the card sweep drops only
  cards of vanished folders
- ``delete_chunks_for_folder`` (full-folder reindex wipe) leaves cards
- dir-meta REST: owner-only writes, viewer reads, empty description deletes
- a queued ``rebuild_folder_cards`` job neither blocks a rename nor lights
  the folder-active pill
- MCP hit shaping: a folder_card payload becomes a ChunkInfo pointer, not
  a crash on the missing file_id
"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import File, Folder, FolderDirMeta
from voitta_rag_enterprise.services import vector_store as vs
from voitta_rag_enterprise.services.indexing import folder_cards


def _seed_folder(root: Path, name: str, rel_paths: list[str]) -> int:
    folder_root = root / name
    folder_root.mkdir(parents=True, exist_ok=True)
    with session_scope() as s:
        folder = Folder(path=str(folder_root), display_name=name)
        s.add(folder)
        s.flush()
        for rel in rel_paths:
            s.add(
                File(
                    folder_id=folder.id,
                    rel_path=rel,
                    last_seen_at=int(time.time()),
                    state="indexed",
                )
            )
        s.flush()
        return folder.id


def _card_subpaths(folder_id: int) -> set[str]:
    return {p["subpath"] for p in vs.list_folder_cards(folder_id)}


def test_rebuild_creates_cards_and_search_finds_them(env, tmp_path: Path) -> None:
    init_db()
    fid = _seed_folder(
        tmp_path,
        "Quarterly Financials",
        [
            "2024/Q3/report.pdf",
            "2024/Q3/annex.xlsx",
            "2024/Q4/report.pdf",
            ".git/objects/aa",  # hidden dir — never carded
            "notes.md.voitta.meta",  # sidecar — excluded
        ],
    )
    result = folder_cards.rebuild_cards_for_folder(fid)
    assert result == {"cards": 4, "changed": True}
    assert _card_subpaths(fid) == {"", "2024", "2024/Q3", "2024/Q4"}

    # Hybrid search on the folder name returns the card (fake sparse embedder
    # is word-hash-based, so name tokens match exactly).
    from voitta_rag_enterprise.services.embedding import (
        get_sparse_embedder,
        get_text_embedder,
    )

    hits = vs.search_chunks(
        dense=get_text_embedder().embed_query("Quarterly Financials"),
        sparse=get_sparse_embedder().embed_query("Quarterly Financials"),
        limit=5,
    )
    assert any(h.payload.get("kind") == "folder_card" for h in hits)


def test_rebuild_is_idempotent_and_diffs(env, tmp_path: Path) -> None:
    init_db()
    fid = _seed_folder(tmp_path, "Docs", ["a/x.md", "b/y.md"])
    assert folder_cards.rebuild_cards_for_folder(fid)["changed"] is True
    # Second run: nothing changed → no embed, no upsert.
    assert folder_cards.rebuild_cards_for_folder(fid) == {
        "cards": 3,
        "changed": False,
    }
    # Remove one subtree's file → its card must drop on the next rebuild.
    with session_scope() as s:
        f = s.execute(select(File).where(File.rel_path == "b/y.md")).scalar_one()
        f.state = "deleted"
    assert folder_cards.rebuild_cards_for_folder(fid)["changed"] is True
    assert _card_subpaths(fid) == {"", "a"}


def test_description_included_in_card_text(env, tmp_path: Path) -> None:
    init_db()
    fid = _seed_folder(tmp_path, "Corp", ["hr/handbook.pdf"])
    with session_scope() as s:
        s.add(
            FolderDirMeta(
                folder_id=fid, subpath="hr", description="Employee onboarding docs"
            )
        )
        # Described-but-absent subpath is still carded.
        s.add(
            FolderDirMeta(
                folder_id=fid, subpath="legal", description="Contracts and NDAs"
            )
        )
    folder_cards.rebuild_cards_for_folder(fid)
    cards = {p["subpath"]: p["text"] for p in vs.list_folder_cards(fid)}
    assert "Employee onboarding docs" in cards["hr"]
    assert "Contracts and NDAs" in cards["legal"]
    assert set(cards) == {"", "hr", "legal"}


def test_clearing_description_removes_it_from_card(env, tmp_path: Path) -> None:
    """Save → rebuild → clear → rebuild: the term must drop out of the card."""
    init_db()
    fid = _seed_folder(tmp_path, "Sandbox", ["a/x.md"])
    with session_scope() as s:
        s.add(FolderDirMeta(folder_id=fid, subpath="", description="walrus taxonomy"))
    folder_cards.rebuild_cards_for_folder(fid)
    texts = {p["subpath"]: p["text"] for p in vs.list_folder_cards(fid)}
    assert "walrus taxonomy" in texts[""]

    with session_scope() as s:
        s.delete(s.get(FolderDirMeta, (fid, "")))
    assert folder_cards.rebuild_cards_for_folder(fid)["changed"] is True
    texts = {p["subpath"]: p["text"] for p in vs.list_folder_cards(fid)}
    assert "walrus taxonomy" not in texts[""]
    assert set(texts) == {"", "a"}  # card itself remains, minus the description


def test_card_cap_breadth_first_and_described_exempt(
    env, tmp_path: Path, monkeypatch
) -> None:
    """Deep trees truncate breadth-first at the cap; described subpaths are
    always carded even past it."""
    init_db()
    monkeypatch.setattr(folder_cards, "_MAX_CARDS_PER_FOLDER", 3)
    fid = _seed_folder(
        tmp_path,
        "Deep",
        [
            "a/x.md",
            "b/x.md",
            "c/x.md",
            "d/x.md",  # 4 shallow dirs > cap of 3
            "a/very/deep/nest/x.md",  # depth loses to breadth
        ],
    )
    with session_scope() as s:
        s.add(
            FolderDirMeta(
                folder_id=fid, subpath="a/very/deep/nest", description="keep me"
            )
        )
    folder_cards.rebuild_cards_for_folder(fid)
    subpaths = _card_subpaths(fid)
    # Root always carded; a/b/c are the breadth-first winners; d and the
    # intermediate deep dirs fell to the cap; the described deep path is
    # exempt from truncation.
    assert subpaths == {"", "a", "b", "c", "a/very/deep/nest"}


def test_enqueue_rebuild_all_covers_every_folder_once(env, tmp_path: Path) -> None:
    """Startup reconciliation: one deduped rebuild job per folder."""
    init_db()
    fids = {
        _seed_folder(tmp_path, "R1", ["a/x.md"]),
        _seed_folder(tmp_path, "R2", []),
    }
    assert folder_cards.enqueue_rebuild_all() == 2
    folder_cards.enqueue_rebuild_all()  # second call must coalesce, not double
    from voitta_rag_enterprise.db.models import Job

    with session_scope() as s:
        jobs = [
            j
            for j in s.execute(select(Job)).scalars()
            if j.kind == "rebuild_folder_cards" and j.state == "queued"
        ]
        import json as _json

        targeted = {int(_json.loads(j.payload)["folder_id"]) for j in jobs}
    assert len(jobs) == 2
    assert targeted == fids


def test_orphan_chunk_sweep_spares_cards(env, tmp_path: Path) -> None:
    init_db()
    fid = _seed_folder(tmp_path, "Kept", ["sub/a.md"])
    folder_cards.rebuild_cards_for_folder(fid)
    assert len(_card_subpaths(fid)) == 2
    # Simulate the startup sweep with NO known chunk ids: every real chunk
    # point would be orphaned — cards must survive untouched.
    deleted = vs.delete_orphan_chunk_points(set())
    assert deleted == 0
    assert len(_card_subpaths(fid)) == 2


def test_card_sweep_drops_only_dead_folders(env, tmp_path: Path) -> None:
    init_db()
    live = _seed_folder(tmp_path, "Live", ["a/x.md"])
    dead = _seed_folder(tmp_path, "Dead", ["b/y.md"])
    folder_cards.rebuild_cards_for_folder(live)
    folder_cards.rebuild_cards_for_folder(dead)
    with session_scope() as s:
        s.delete(s.get(Folder, dead))
    assert folder_cards.sweep_orphan_cards() == 2  # root + 'b'
    assert len(_card_subpaths(live)) == 2
    assert _card_subpaths(dead) == set()


def test_reindex_folder_wipe_spares_cards(env, tmp_path: Path) -> None:
    init_db()
    fid = _seed_folder(tmp_path, "Reindexed", ["a/x.md"])
    folder_cards.rebuild_cards_for_folder(fid)
    vs.delete_chunks_for_folder(fid)  # the reindex wipe path
    assert _card_subpaths(fid) == {"", "a"}


def test_rebuild_drops_cards_when_folder_gone(env, tmp_path: Path) -> None:
    init_db()
    fid = _seed_folder(tmp_path, "Gone", ["a/x.md"])
    folder_cards.rebuild_cards_for_folder(fid)
    with session_scope() as s:
        s.delete(s.get(Folder, fid))
    result = folder_cards.rebuild_cards_for_folder(fid)
    assert result["changed"] is True
    assert _card_subpaths(fid) == set()


def test_card_job_untracked_by_folder_active_and_rename_gate(
    env, tmp_path: Path
) -> None:
    init_db()
    from voitta_rag_enterprise.api.routes.folders import _folder_has_active_job
    from voitta_rag_enterprise.services import folder_active

    # ``folder_active._counts`` is process-global — flush any leftovers
    # from earlier tests before asserting on membership.
    folder_active.init_from_db()

    fid = _seed_folder(tmp_path, "Renamable", ["a/x.md"])
    with session_scope() as s:
        folder_cards.enqueue_rebuild(s, fid)
    # Not counted as folder activity (live on_enqueued path)...
    assert fid not in folder_active.get_active_ids()
    # ...nor by the DB bootstrap recount.
    folder_active.init_from_db()
    assert fid not in folder_active.get_active_ids()
    # ...and doesn't block a physical rename.
    with session_scope() as s:
        assert _folder_has_active_job(s, fid) is False


def test_dir_meta_rest_roundtrip(app, client, tmp_path: Path) -> None:
    from tests.conftest import auth_as

    auth_as(app, "owner@example.com")
    r = client.post("/api/folders", json={"name": "described"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]

    # Owner writes root + subdir descriptions.
    r = client.put(
        f"/api/folders/{fid}/dir-meta",
        json={"subpath": "", "description": "Root docs"},
    )
    assert r.status_code == 200, r.text
    r = client.put(
        f"/api/folders/{fid}/dir-meta",
        json={"subpath": "sub/dir", "description": "Nested things"},
    )
    assert r.status_code == 200
    rows = client.get(f"/api/folders/{fid}/dir-meta").json()
    assert {(x["subpath"], x["description"]) for x in rows} == {
        ("", "Root docs"),
        ("sub/dir", "Nested things"),
    }

    # Traversal is rejected.
    r = client.put(
        f"/api/folders/{fid}/dir-meta",
        json={"subpath": "../evil", "description": "x"},
    )
    assert r.status_code == 400

    # Empty description deletes the row.
    r = client.put(
        f"/api/folders/{fid}/dir-meta", json={"subpath": "", "description": "  "}
    )
    assert r.status_code == 200
    rows = client.get(f"/api/folders/{fid}/dir-meta").json()
    assert [x["subpath"] for x in rows] == ["sub/dir"]

    # Non-owner cannot write (invisible folder → 404, not 403 leak).
    auth_as(app, "other@example.com")
    r = client.put(
        f"/api/folders/{fid}/dir-meta", json={"subpath": "", "description": "hax"}
    )
    assert r.status_code == 404
    assert client.get(f"/api/folders/{fid}/dir-meta").status_code == 404
    # Save enqueued a card-rebuild job for the owner's writes.
    from voitta_rag_enterprise.db.models import Job

    with session_scope() as s:
        kinds = {j.kind for j in s.execute(select(Job)).scalars()}
    assert "rebuild_folder_cards" in kinds


def test_rest_search_returns_folder_card(app, client, tmp_path: Path) -> None:
    """POST /api/search must serialize a folder-card hit (UUID point id).

    Regression: ``Hit.id`` was ``int``-typed, so the first card hit made
    the endpoint 500 with a pydantic int_parsing error.
    """
    from tests.conftest import auth_as

    auth_as(app, "owner@example.com")
    r = client.post("/api/folders", json={"name": "cardsearch"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    with session_scope() as s:
        s.add(
            File(
                folder_id=fid,
                rel_path="quarterly/report.md",
                last_seen_at=int(time.time()),
                state="indexed",
            )
        )
    folder_cards.rebuild_cards_for_folder(fid)

    r = client.post(
        "/api/search",
        json={"query": "cardsearch quarterly", "modes": ["chunks"], "limit": 10},
    )
    assert r.status_code == 200, r.text
    hits = r.json()["chunks"]
    cards = [h for h in hits if h["payload"].get("kind") == "folder_card"]
    assert cards, f"no folder_card hit in {[h['payload'].get('kind') for h in hits]}"
    assert isinstance(cards[0]["id"], str)  # UUID point id survives the model


def test_mcp_chunk_from_hit_handles_folder_card(env) -> None:
    from voitta_rag_enterprise.mcp_server import _chunk_from_hit
    from voitta_rag_enterprise.services.vector_store import SearchHit

    hit = SearchHit(
        id="4fe0a832-0000-5000-8000-1234567890ab",
        score=0.42,
        payload={
            "kind": "folder_card",
            "folder_id": 7,
            "subpath": "2024/Q3",
            "display_name": "Quarterly Financials",
            "text": "Folder: Quarterly Financials\nPath: Quarterly Financials / 2024 / Q3",
        },
    )
    info = _chunk_from_hit(hit)
    assert info.kind == "folder_card"
    assert info.file_id is None
    assert info.folder_id == 7
    assert info.file_path == "2024/Q3"
    assert "Quarterly Financials" in info.text
    assert info.source_kind == "folder"


def test_reembed_stale_scan_handles_cards(env, tmp_path: Path) -> None:
    """A stale-version card routes to a rebuild job, not a KeyError."""
    init_db()
    fid = _seed_folder(tmp_path, "Versioned", ["a/x.md"])
    folder_cards.rebuild_cards_for_folder(fid)

    from scripts.reembed_stale import _scan_for_stale

    stale = _scan_for_stale(
        vs.CHUNKS,
        ["dense_model_version", "sparse_model_version"],
        {
            "dense_model_version": "next-model@2",
            "sparse_model_version": "bm25@1",
        },
    )
    assert ("folder_cards", fid) in stale
