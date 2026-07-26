"""Folder-card builder — makes folder/subfolder names + descriptions searchable.

A *folder card* is a synthetic point in the Qdrant ``chunks`` collection
(``kind='folder_card'``) whose text is the folder's display name, the
subfolder path words, and the optional user-written description from
``folder_dir_meta``. Cards ride the same hybrid (e5 + BM25, RRF-fused)
search as document chunks, so one query hits content and folder names
alike; the ``folder_id`` payload keeps the existing ACL filter working
unchanged.

Lifecycle: ``rebuild_cards_for_folder`` is idempotent and cheap when
nothing changed — it diffs the desired (subpath → text/model-version)
set against what Qdrant holds and only embeds + upserts on a real
difference. Callers therefore enqueue ``rebuild_folder_cards`` jobs
liberally (folder create/rename, dir-meta save, upload/mkdir/subdir
delete, sync completion, startup) and rely on the dedup key +
change detection to keep the actual work rare.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from ...config import get_settings
from ...db.database import session_scope
from ...db.models import File, Folder, FolderDirMeta
from ...logging_config import bind_context
from .common import logger

# Namespace for the deterministic card point ids: uuid5(ns, f"{folder_id}:{subpath}").
# Determinism means a re-embed replaces the point instead of duplicating it,
# and the id survives process restarts / resumes. The value is arbitrary but
# MUST never change — changing it would orphan every existing card point.
_CARD_NS = uuid.UUID("b0a3d1e4-7c55-4d2a-9f1e-3f8a2c6d9b41")

# Cap on cards per folder. Deep mirrors (a synced repo, a Drive export) can
# hold thousands of subdirectories whose short name-tokens would pollute BM25
# results; breadth-first truncation keeps the shallow — most navigational —
# levels. Subpaths with a user description are always carded regardless.
_MAX_CARDS_PER_FOLDER = 200


def card_point_id(folder_id: int, subpath: str) -> str:
    return str(uuid.uuid5(_CARD_NS, f"{folder_id}:{subpath}"))


def card_text(display_name: str, subpath: str, description: str) -> str:
    """Human/embedder-facing card body.

    The path words are the searchable "name" signal; the description (when
    present) adds the semantic meat. e5 + BM25 both see one plain-text blob.
    """
    lines = [f"Folder: {display_name}"]
    if subpath:
        lines.append(f"Path: {display_name} / {' / '.join(subpath.split('/'))}")
    if description:
        lines.append(description)
    return "\n".join(lines)


def enqueue_rebuild(session, folder_id: int) -> None:
    """Enqueue a (deduped) card rebuild for ``folder_id``.

    Deliberately fire-and-forget: callers invoke this after their own
    commit-worthy mutation; the handler self-heals from any state.
    """
    from .. import job_queue

    job_queue.enqueue(
        session,
        "rebuild_folder_cards",
        {"folder_id": folder_id},
        dedup_key=f"folder_cards:{folder_id}",
    )


async def run_rebuild_folder_cards(payload: dict) -> dict | None:
    folder_id = int(payload["folder_id"])
    try:
        return await asyncio.to_thread(rebuild_cards_for_folder, folder_id)
    except Exception:
        with bind_context(folder_id=folder_id):
            logger.exception("rebuild_folder_cards failed")
        raise


def _desired_cards(session, folder: Folder) -> dict[str, str]:
    """Compute ``subpath → card text`` for a folder.

    Root ('') is always carded. Subdirectories come from the distinct
    parent-dir prefixes of live file rel_paths (dot-dirs excluded — same
    hiding rule as the MCP directory listing), truncated breadth-first at
    ``_MAX_CARDS_PER_FOLDER``. Described subpaths are always included,
    even when past the cap or currently empty on disk.
    """
    rel_paths = session.execute(
        select(File.rel_path).where(
            File.folder_id == folder.id,
            File.state != "deleted",
            ~File.rel_path.like("%.voitta.meta"),
        )
    ).scalars()

    subdirs: set[str] = set()
    for rel in rel_paths:
        parts = rel.split("/")[:-1]  # parent dirs only
        if any(p.startswith(".") for p in parts):
            continue
        for i in range(1, len(parts) + 1):
            subdirs.add("/".join(parts[:i]))

    descriptions: dict[str, str] = {
        row.subpath: row.description.strip()
        for row in session.execute(
            select(FolderDirMeta).where(FolderDirMeta.folder_id == folder.id)
        ).scalars()
        if row.description.strip()
    }

    # Breadth-first: shallow (navigational) dirs win the cap.
    ordered = sorted(subdirs, key=lambda p: (p.count("/"), p))
    if len(ordered) > _MAX_CARDS_PER_FOLDER:
        logger.info(
            "folder_cards: folder %d has %d subdirs, carding first %d "
            "(breadth-first) + %d described",
            folder.id,
            len(ordered),
            _MAX_CARDS_PER_FOLDER,
            len(descriptions),
        )
        ordered = ordered[:_MAX_CARDS_PER_FOLDER]

    wanted = {"", *ordered, *descriptions.keys()}
    return {
        sp: card_text(folder.display_name, sp, descriptions.get(sp, ""))
        for sp in wanted
    }


def rebuild_cards_for_folder(folder_id: int) -> dict | None:
    """Recompute + replace the folder-card points for ``folder_id``.

    No-ops (cheaply) when the stored cards already match the desired set —
    compared on (subpath, text, dense/sparse model version) — so liberal
    triggering never causes redundant embedding work. A vanished folder
    drops its cards.
    """
    from .. import vector_store
    from ..acl import folder_user_ids
    from ..embedding import get_sparse_embedder, get_text_embedder

    settings = get_settings()
    with bind_context(folder_id=folder_id):
        with session_scope() as s:
            folder = s.get(Folder, folder_id)
            if folder is None:
                vector_store.delete_cards_for_folder(folder_id)
                logger.info("folder_cards: folder gone, cards dropped")
                return {"cards": 0, "changed": True}
            desired = _desired_cards(s, folder)
            display_name = folder.display_name
            allowed_users = folder_user_ids(s, folder_id)

        existing = {
            p.get("subpath"): (
                p.get("text"),
                p.get("dense_model_version"),
                p.get("sparse_model_version"),
            )
            for p in vector_store.list_folder_cards(folder_id)
        }
        wanted = {
            sp: (text, settings.dense_version, settings.sparse_version)
            for sp, text in desired.items()
        }
        if existing == wanted:
            logger.debug("folder_cards: unchanged (%d cards)", len(wanted))
            return {"cards": len(wanted), "changed": False}

        text_emb = get_text_embedder()
        sparse_emb = get_sparse_embedder()
        vector_store.ensure_chunks_collection(text_dim=text_emb.dim)

        subpaths = sorted(desired.keys())
        texts = [desired[sp] for sp in subpaths]
        denses = text_emb.embed_documents(texts)
        sparses = sparse_emb.embed_documents(texts)
        points = [
            vector_store.FolderCardPoint(
                point_id=card_point_id(folder_id, sp),
                folder_id=folder_id,
                subpath=sp,
                display_name=display_name,
                text=text,
                dense=dense,
                sparse=sparse,
                dense_model_version=settings.dense_version,
                sparse_model_version=settings.sparse_version,
                allowed_users=allowed_users,
            )
            for sp, text, dense, sparse in zip(
                subpaths, texts, denses, sparses, strict=True
            )
        ]
        vector_store.replace_cards_for_folder(folder_id, points)
        logger.info("folder_cards: rebuilt %d card(s)", len(points))
        return {"cards": len(points), "changed": True}


def sweep_orphan_cards() -> int:
    """Drop card points of folders deleted while the process was down."""
    from .. import vector_store

    with session_scope() as s:
        live = {fid for (fid,) in s.execute(select(Folder.id)).all()}
    return vector_store.sweep_orphan_folder_cards(live)


def enqueue_rebuild_all() -> int:
    """Enqueue a card rebuild for every folder (startup reconciliation).

    Cheap by design: each job no-ops via the rebuilder's change detection
    unless the folder tree / descriptions actually changed while we were
    down. Returns the number of folders enqueued.
    """
    with session_scope() as s:
        folder_ids = [fid for (fid,) in s.execute(select(Folder.id)).all()]
        for fid in folder_ids:
            enqueue_rebuild(s, fid)
    return len(folder_ids)
