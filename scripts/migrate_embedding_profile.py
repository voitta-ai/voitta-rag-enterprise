"""Move an install to another embedding profile without re-parsing anything.

    python scripts/migrate_embedding_profile.py --to gemma2 --dry-run
    python scripts/migrate_embedding_profile.py --to gemma2
    # restart the app; it now serves gemma2
    python scripts/migrate_embedding_profile.py --to gemma2   # catch-up, idempotent
    python scripts/migrate_embedding_profile.py --to gemma2 --drop-old   # later

What it does (docs/plans/gemma-migration.md, "Migration command"):

1. Marks the migration in progress (``app_settings.embedding_migration``) so
   an app restart mid-way never switches to half-filled collections.
2. Re-embeds every chunk from the text already in SQL and every image blob
   from the CAS into the TARGET profile's collections, through the same
   code the indexer uses (``embed_file_chunks`` / ``embed_image_targets``),
   then rebuilds folder cards and sweeps target-side orphans. It diffs the
   target against SQL first, so an interrupted run resumes where it
   stopped, and it repeats until a pass finds nothing to do (files the
   running app re-indexed meanwhile are picked up).
3. Records the target as the deployment's profile (``app_settings``) and
   clears the in-progress mark. The app switches on its next restart. The
   source collections are kept for rollback until ``--drop-old``.

Runs beside the live app with standalone Qdrant: it reads SQL + CAS, writes
only the target collections, and takes ``gpu_lock`` per batch like the
indexer. Rollback at any point before step 3 is "stop it"; the source profile
keeps serving. Embedded and managed Qdrant belong to one process: stop the
app first.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import defaultdict

from sqlalchemy import func, select

from voitta_rag_enterprise.config import get_settings
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, CollectionMeta, Folder, Image
from voitta_rag_enterprise.services import vector_store
from voitta_rag_enterprise.services.embedding import profiles
from voitta_rag_enterprise.services.embedding.image import UnsupportedImageError

logger = logging.getLogger("migrate_embedding_profile")

# Measured on an RTX 5070 (docs/plans/gemma-migration.md): for --dry-run only.
_THROUGHPUT = {
    "gemma2": {"chunks_per_s": 128.0, "images_per_s": 8.0},
    "e5-siglip": {"chunks_per_s": 466.0, "images_per_s": 222.0},
}
_IMAGE_BATCH = 64
_MAX_PASSES = 5


def _other(name: str) -> str:
    (other,) = (p for p in profiles.PROFILE_NAMES if p != name)
    return other


# ---------------------------------------------------------------------------
# diff SQL against the target collections
# ---------------------------------------------------------------------------


def _files_to_embed() -> list[int]:
    """Files whose stored chunks differ from the target's chunk points, or
    whose points carry another model version."""
    version = profiles.active().dense_version
    want: dict[int, set[int]] = defaultdict(set)
    with session_scope() as s:
        for file_id, chunk_id in s.execute(select(Chunk.file_id, Chunk.id)):
            want[file_id].add(chunk_id)
    have: dict[int, set[int]] = defaultdict(set)
    stale: set[int] = set()
    fields = ["file_id", "chunk_id", "kind", "dense_model_version"]
    for p in vector_store.scan_payload_fields(vector_store.chunks_collection(), fields):
        if p.get("kind") == vector_store.FOLDER_CARD_KIND or "chunk_id" not in p:
            continue
        have[int(p["file_id"])].add(int(p["chunk_id"]))
        if p.get("dense_model_version") != version:
            stale.add(int(p["file_id"]))
    return sorted(fid for fid, ids in want.items() if have.get(fid) != ids or fid in stale)


def _images_to_embed() -> list[int]:
    """Image rows whose content is not in the target, or whose file is not
    attached to the point holding that content."""
    version = profiles.active().image_version
    have: dict[str, set[int]] = defaultdict(set)
    fields = ["image_cas_id", "file_ids", "image_model_version"]
    for p in vector_store.scan_payload_fields(vector_store.images_collection(), fields):
        if p.get("image_model_version") == version:
            have[str(p.get("image_cas_id"))].update(int(f) for f in p.get("file_ids") or [])
    with session_scope() as s:
        rows = s.execute(select(Image.id, Image.file_id, Image.image_cas_id)).all()
    return [iid for iid, fid, cas in rows if fid not in have.get(cas, set())]


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------


def _pass(n: int) -> int:
    from voitta_rag_enterprise.services.indexing.embed import (
        embed_file_chunks,
        embed_image_targets,
        load_image_target,
    )
    from voitta_rag_enterprise.services.indexing.folder_cards import rebuild_cards_for_folder

    work = 0
    files = _files_to_embed()
    logger.info("pass %d: %d file(s) to embed", n, len(files))
    t0 = time.monotonic()
    for i, fid in enumerate(files, 1):
        embed_file_chunks(fid)
        work += 1
        if i % 100 == 0 or i == len(files):
            logger.info("  files %d/%d (%.0fs)", i, len(files), time.monotonic() - t0)

    images = _images_to_embed()
    logger.info("pass %d: %d image row(s) to embed or attach", n, len(images))
    t0 = time.monotonic()
    failed: list[int] = []
    for start in range(0, len(images), _IMAGE_BATCH):
        targets = [t for iid in images[start : start + _IMAGE_BATCH] if (t := load_image_target(iid))]
        try:
            embed_image_targets(targets)
        except UnsupportedImageError:
            # One undecodable blob must not sink its batch: redo one by one.
            for t in targets:
                try:
                    embed_image_targets([t])
                except UnsupportedImageError as e:
                    failed.append(t.image_id)
                    logger.warning("  image %d skipped: %s", t.image_id, e)
        work += len(targets)
        done = min(start + _IMAGE_BATCH, len(images))
        logger.info("  images %d/%d (%.0fs)", done, len(images), time.monotonic() - t0)

    with session_scope() as s:
        folder_ids = list(s.execute(select(Folder.id)).scalars())
        chunk_ids = set(s.execute(select(Chunk.id)).scalars())
        image_ids = set(s.execute(select(Image.id)).scalars())
    for fid in folder_ids:
        res = rebuild_cards_for_folder(fid)
        work += bool(res and res.get("changed"))
    swept = vector_store.delete_orphan_chunk_points(chunk_ids) + vector_store.delete_orphan_image_points(image_ids)
    if swept:
        logger.info("pass %d: swept %d orphan point(s)", n, swept)
    if failed:
        logger.warning("pass %d: %d image(s) could not be decoded (also unindexed before)", n, len(failed))
    # Undecodable images reappear every pass; they are not outstanding work.
    return work - len(failed)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _dry_run(target: str) -> None:
    with session_scope() as s:
        n_files = s.execute(select(func.count(func.distinct(Chunk.file_id)))).scalar_one()
        n_chunks = s.execute(select(func.count(Chunk.id))).scalar_one()
        n_images = s.execute(select(func.count(func.distinct(Image.image_cas_id)))).scalar_one()
    todo_files, todo_images = _files_to_embed(), _images_to_embed()
    with session_scope() as s:
        todo_chunks = s.execute(
            select(func.count(Chunk.id)).where(Chunk.file_id.in_(todo_files))
        ).scalar_one() if todo_files else 0
    tp = _THROUGHPUT[target]
    eta = todo_chunks / tp["chunks_per_s"] + len(todo_images) / tp["images_per_s"]
    print(f"target profile : {target}  ({vector_store.chunks_collection()}, {vector_store.images_collection()})")
    print(f"corpus         : {n_files} files, {n_chunks} chunks, {n_images} distinct images")
    print(f"still to do    : {len(todo_files)} files ({todo_chunks} chunks), {len(todo_images)} image rows")
    print(f"estimate       : ~{eta / 3600:.1f} h at RTX 5070 rates (upper bound: image rows include duplicates)")


def _drop_old(target: str) -> int:
    old = profiles.profile(_other(target))
    if profiles.read_stored_profile() != target or profiles.read_setting(profiles.MIGRATION_SETTING_KEY):
        print(f"refusing: the deployment is not on a completed {target} migration", file=sys.stderr)
        return 2

    def _drop() -> None:
        client = vector_store.get_client()
        for name in (old.chunks_collection, old.images_collection):
            if client.collection_exists(name):
                client.delete_collection(name)
                logger.info("dropped %s", name)

    vector_store.run_on_qdrant(_drop)
    with session_scope() as s:
        for name in (old.chunks_collection, old.images_collection):
            if row := s.get(CollectionMeta, name):
                s.delete(row)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, choices=profiles.PROFILE_NAMES)
    ap.add_argument("--dry-run", action="store_true", help="report the work and an estimate, change nothing")
    ap.add_argument("--drop-old", action="store_true", help="after a completed migration: delete the old profile's collections")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    init_db()
    target = args.to
    # This process works on the target's collections and models by design,
    # while the app keeps serving the source profile.
    profiles.pin(target)
    if args.drop_old:
        return _drop_old(target)
    if args.dry_run:
        _dry_run(target)
        return 0

    if get_settings().qdrant_mode != "standalone":
        logger.warning(
            "%s Qdrant: make sure the app is stopped (the store belongs to one process)",
            get_settings().qdrant_mode,
        )
    # A catch-up run on a deployment already switched to the target must not
    # mark it in progress: the app would refuse to start meanwhile.
    switched = profiles.read_stored_profile() == target and not profiles.read_setting(
        profiles.MIGRATION_SETTING_KEY
    )
    if not switched:
        profiles.write_setting(profiles.MIGRATION_SETTING_KEY, target)
    started = time.monotonic()
    for n in range(1, _MAX_PASSES + 1):
        if _pass(n) == 0:
            break
    else:
        logger.error("still changing after %d passes; not switching. Re-run when indexing is quiet.", _MAX_PASSES)
        return 1
    profiles.write_stored_profile(target)
    profiles.write_setting(profiles.MIGRATION_SETTING_KEY, None)
    logger.info("migration to %s complete in %.0fs; restart the app to switch", target, time.monotonic() - started)
    env = os.environ.get("VOITTA_EMBEDDING_PROFILE")
    if env and env != target:
        logger.warning("VOITTA_EMBEDDING_PROFILE=%s is set and wins over the recorded profile; change it to %s", env, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
