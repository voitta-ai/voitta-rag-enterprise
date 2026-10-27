"""Move a deployment to another embedding profile without re-parsing anything.

    python scripts/migrate_embedding_profile.py --to gemma2 --dry-run
    python scripts/migrate_embedding_profile.py --to gemma2            # app keeps running
    # stop the app
    python scripts/migrate_embedding_profile.py --to gemma2 --finish   # app stopped
    # start the app: it now serves gemma2
    python scripts/migrate_embedding_profile.py --to gemma2 --drop-old # later, optional

Rolling back is the same command with the other profile (``--to e5-siglip``):
the old collections were kept, so only what changed since is re-embedded.

Phases (docs/plans/gemma-migration.md, "Migration command"):

* **bulk** (default; the app may be running and keeps serving the current
  profile). Marks the migration in progress (``app_settings``), then
  re-embeds every chunk from the text already in SQL and every figure from
  the CAS into the TARGET profile's collections, through the same code the
  indexer uses. It diffs the target against SQL first (chunk ids, content
  hashes, version stamps), so an interrupted run resumes where it stopped.
  The app never writes the target collections while it serves another
  profile, so this phase may also sweep them.
* **--finish** (the app must be stopped). One more pass picks up what the
  app indexed during the bulk phase; it repeats until a pass finds nothing
  left. Then, in one transaction, it records the target as the
  deployment's profile and clears the in-progress mark. Items that failed
  (undecodable image, missing blob) are listed and block the switch unless
  ``--accept-failures``.
* **--drop-old** deletes the previous profile's collections once the
  deployment serves the target.

GPU: this is a separate process, so the app's ``gpu_lock`` does not cover
it. Beside a running app on one GPU, the target model (EmbeddingGemma-2
~2.2 GB peak) comes on top of what the app holds (its embedders and the
MinerU daemon); run the bulk phase when indexing is quiet or check
``nvidia-smi`` headroom first. With embedded Qdrant only one process can
open the store, so the app must be stopped for every phase.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select

from voitta_rag_enterprise.config import get_settings
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Chunk, CollectionMeta, Folder, Image
from voitta_rag_enterprise.services import vector_store
from voitta_rag_enterprise.services.embedding import profiles

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
    """Files whose chunks in SQL differ from the target's points — by chunk
    id, by content hash (SQLite reuses chunk ids when a file is re-chunked)
    or by model version stamp."""
    version = profiles.active().dense_version
    want: dict[int, set[tuple[int, str]]] = defaultdict(set)
    with session_scope() as s:
        for file_id, chunk_id, chunk_hash in s.execute(select(Chunk.file_id, Chunk.id, Chunk.chunk_hash)):
            want[file_id].add((chunk_id, chunk_hash))
    have: dict[int, set[tuple[int, str]]] = defaultdict(set)
    stale: set[int] = set()
    fields = ["file_id", "chunk_id", "chunk_hash", "kind", "dense_model_version"]
    for p in vector_store.scan_payload_fields(vector_store.chunks_collection(), fields):
        if p.get("kind") == vector_store.FOLDER_CARD_KIND or "chunk_id" not in p:
            continue
        fid = int(p["file_id"])
        have[fid].add((int(p["chunk_id"]), p.get("chunk_hash")))
        if p.get("dense_model_version") != version:
            stale.add(fid)
    return sorted(fid for fid, ids in want.items() if have.get(fid) != ids or fid in stale)


def _images_to_embed() -> list[int]:
    """Figure rows whose content is not in the target with the current
    version stamp, or whose file is not attached to the point holding it.
    Page renders are never embedded (the indexer does not embed them)."""
    version = profiles.active().image_version
    have: dict[str, set[int]] = defaultdict(set)
    fields = ["image_cas_id", "file_ids", "image_model_version"]
    for p in vector_store.scan_payload_fields(vector_store.images_collection(), fields):
        if p.get("image_model_version") == version:
            have[str(p.get("image_cas_id"))].update(int(f) for f in p.get("file_ids") or [])
    with session_scope() as s:
        rows = s.execute(
            select(Image.id, Image.file_id, Image.image_cas_id).where(Image.kind == "figure")
        ).all()
    return [iid for iid, fid, cas in rows if fid not in have.get(cas, set())]


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------


@dataclass
class PassResult:
    work: int = 0  # items embedded, attached, re-stamped, swept or rebuilt
    failed: dict[str, str] = field(default_factory=dict)  # "file 12" / "image 7" -> error


def _pass(n: int, known_failures: set[str]) -> PassResult:
    from voitta_rag_enterprise.services.indexing.embed import (
        embed_file_chunks,
        embed_image_targets,
        load_image_target,
    )
    from voitta_rag_enterprise.services.indexing.folder_cards import rebuild_cards_for_folder

    out = PassResult()
    # An item that already failed this run would fail again: reported once,
    # not counted as outstanding work.
    files = [f for f in _files_to_embed() if f"file {f}" not in known_failures]
    logger.info("pass %d: %d file(s) to embed", n, len(files))
    t0 = time.monotonic()
    for i, fid in enumerate(files, 1):
        try:
            embed_file_chunks(fid)
            out.work += 1
        except Exception as e:
            out.failed[f"file {fid}"] = f"{type(e).__name__}: {e}"
            logger.warning("  file %d failed: %s", fid, e)
        if i % 100 == 0 or i == len(files):
            logger.info("  files %d/%d (%.0fs)", i, len(files), time.monotonic() - t0)

    images = [i for i in _images_to_embed() if f"image {i}" not in known_failures]
    logger.info("pass %d: %d figure row(s) to embed or attach", n, len(images))
    t0 = time.monotonic()
    for start in range(0, len(images), _IMAGE_BATCH):
        targets = []
        for iid in images[start : start + _IMAGE_BATCH]:
            t = load_image_target(iid)
            if t is not None:
                targets.append(t)
        try:
            embed_image_targets(targets)
            out.work += len(targets)
        except Exception:
            for t in targets:
                try:
                    embed_image_targets([t])
                    out.work += 1
                except Exception as e:
                    out.failed[f"image {t.image_id}"] = f"{type(e).__name__}: {e}"
                    logger.warning("  image %d failed: %s", t.image_id, e)
        done = min(start + _IMAGE_BATCH, len(images))
        logger.info("  figures %d/%d (%.0fs)", done, len(images), time.monotonic() - t0)

    # The app does not write the target's collections while it serves
    # another profile, so cards and sweeps here never race a live writer.
    with session_scope() as s:
        folder_ids = list(s.execute(select(Folder.id)).scalars())
    for fid in folder_ids:
        res = rebuild_cards_for_folder(fid)
        out.work += bool(res and res.get("changed"))
    with session_scope() as s:
        chunk_ids = set(s.execute(select(Chunk.id)).scalars())
        live_images = dict(
            s.execute(select(Image.id, Image.image_cas_id).where(Image.kind == "figure")).all()
        )
    swept = vector_store.delete_orphan_chunk_points(chunk_ids) + vector_store.delete_orphan_image_points(
        live_images
    )
    if swept:
        logger.info("pass %d: swept %d orphan point(s)", n, swept)
    out.work += swept
    return out


def _run_passes() -> tuple[bool, dict[str, str]]:
    """Passes until one finds nothing to do. Returns (converged, failures)."""
    failures: dict[str, str] = {}
    for n in range(1, _MAX_PASSES + 1):
        res = _pass(n, set(failures))
        failures.update(res.failed)
        if res.work == 0:
            return True, failures
    return False, failures


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _dry_run(target: str) -> None:
    with session_scope() as s:
        n_files = s.execute(select(func.count(func.distinct(Chunk.file_id)))).scalar_one()
        n_chunks = s.execute(select(func.count(Chunk.id))).scalar_one()
        n_images = s.execute(
            select(func.count(func.distinct(Image.image_cas_id))).where(Image.kind == "figure")
        ).scalar_one()
    todo_files, todo_images = _files_to_embed(), _images_to_embed()
    with session_scope() as s:
        todo_chunks = (
            s.execute(select(func.count(Chunk.id)).where(Chunk.file_id.in_(todo_files))).scalar_one()
            if todo_files
            else 0
        )
        todo_distinct = (
            s.execute(
                select(func.count(func.distinct(Image.image_cas_id))).where(Image.id.in_(todo_images))
            ).scalar_one()
            if todo_images
            else 0
        )
    tp = _THROUGHPUT[target]
    eta = todo_chunks / tp["chunks_per_s"] + todo_distinct / tp["images_per_s"]
    print(f"target profile : {target}  ({vector_store.chunks_collection()}, {vector_store.images_collection()})")
    print(f"served now     : {profiles.read_stored_profile() or '(not recorded yet)'}")
    print(f"corpus         : {n_files} files, {n_chunks} chunks, {n_images} distinct figures")
    print(
        f"still to do    : {len(todo_files)} files ({todo_chunks} chunks), {todo_distinct} distinct figures"
        f" ({len(todo_images)} figure rows)"
    )
    print(f"estimate       : ~{eta / 3600:.1f} h at RTX 5070 rates")


def _drop_old(target: str) -> int:
    old = profiles.profile(_other(target))
    if profiles.read_stored_profile() != target or profiles.read_setting(profiles.MIGRATION_SETTING_KEY):
        print(f"refusing: the deployment does not serve {target} yet (finish the migration first)", file=sys.stderr)
        return 2
    counts = vector_store.collection_point_counts([vector_store.chunks_collection()])
    if not counts.get(vector_store.chunks_collection()):
        print(f"refusing: {vector_store.chunks_collection()} is empty", file=sys.stderr)
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


def _report_failures(failures: dict[str, str]) -> None:
    if not failures:
        return
    logger.warning("%d item(s) could not be embedded:", len(failures))
    for key, err in sorted(failures.items()):
        logger.warning("  %s: %s", key, err[:300])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, choices=profiles.PROFILE_NAMES)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report the work and an estimate, change nothing")
    mode.add_argument("--finish", action="store_true", help="final pass and switch; the app must be stopped")
    mode.add_argument("--drop-old", action="store_true", help="delete the previous profile's collections")
    ap.add_argument("--accept-failures", action="store_true", help="with --finish: switch even if some items failed")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    init_db()
    target = args.to
    served = profiles.read_stored_profile()
    # This process works on the target's collections and models by design,
    # while the app keeps serving its recorded profile.
    profiles.pin(target)
    if args.drop_old:
        return _drop_old(target)
    if args.dry_run:
        _dry_run(target)
        return 0
    if served == target:
        logger.info("the deployment already serves %s; nothing to migrate", target)
        return 0
    if served is None:
        logger.error("no embedding profile recorded yet: start the app once so it records the one it serves")
        return 2
    if get_settings().qdrant_mode == "embedded":
        logger.warning("embedded Qdrant: the app must be stopped (one process per store)")

    profiles.write_setting(profiles.MIGRATION_SETTING_KEY, target)
    started = time.monotonic()
    converged, failures = _run_passes()
    _report_failures(failures)
    if not args.finish:
        logger.info(
            "bulk phase done in %.0fs%s. Stop the app, then run with --finish.",
            time.monotonic() - started,
            "" if converged else " (still changing: the app is indexing)",
        )
        return 0
    if not converged:
        logger.error("still changing after %d passes with --finish: is the app really stopped?", _MAX_PASSES)
        return 1
    if failures and not args.accept_failures:
        logger.error("not switching: %d item(s) failed (re-run, or pass --accept-failures)", len(failures))
        return 1
    profiles.write_settings({profiles.PROFILE_SETTING_KEY: target, profiles.MIGRATION_SETTING_KEY: None})
    logger.info("migration to %s complete in %.0fs; start the app to serve it", target, time.monotonic() - started)
    env = get_settings().embedding_profile
    if env and env != target:
        logger.warning(
            "VOITTA_EMBEDDING_PROFILE=%s is set in this environment; the app will refuse to start "
            "until it is %s or unset",
            env,
            target,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
