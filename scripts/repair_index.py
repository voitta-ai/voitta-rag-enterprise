"""Check SQL against Qdrant, and repair the drift without re-parsing.

Usage::

    python -m scripts.repair_index                    # check (read-only)
    python -m scripts.repair_index --folder "McKinsey Quarterly"
    python -m scripts.repair_index --repair           # queue fixes, return
    python -m scripts.repair_index --repair --wait    # ... and wait, re-check, repeat

``--repair`` queues the app's own embed jobs (the app must be running to
work them off) and deletes points whose rows are gone from SQL. A blob
shared by several files needs two passes (embed, then attach); ``--wait``
does them. See ``services/index_repair.py`` for what each category means.

Exits 0 when the index is consistent, 1 when drift remains, 2 when the
check or the repair was refused (e.g. the Qdrant it reached is not this
database's: a missing collection is reported, not taken as empty).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict

from sqlalchemy import select

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Folder, Job
from voitta_rag_enterprise.services import index_repair
from voitta_rag_enterprise.services.embedding import profiles

logger = logging.getLogger(__name__)


def _resolve_folders(names: list[str]) -> list[int] | None:
    if not names:
        return None
    with session_scope() as s:
        folders = list(s.execute(select(Folder)).scalars())
    ids = []
    for n in names:
        hit = [f.id for f in folders if str(f.id) == n or f.display_name == n]
        if not hit:
            sys.exit(f"no folder {n!r}")
        ids.extend(hit)
    return ids


def _print(report: index_repair.IndexReport, *, verbose: bool) -> None:
    rows = [r for r in report.folders if verbose or not r.ok]
    print(f"Qdrant: {report.qdrant}")
    print(
        f"SQL chunks {report.sql_chunks:,} · chunk points {report.chunk_points:,} · "
        f"image points {report.image_points:,}"
    )
    if rows:
        print(
            f"\n{'folder':<36} {'files':>7} {'chunks':>9} {'missing':>9} {'stale':>7} "
            f"{'images':>7} {'img miss':>9} {'unattach':>9} {'img stale':>9} {'empty':>6}"
        )
        for r in rows:
            mark = "ok " if r.ok else "!! "
            print(
                f"{mark}{r.name[:33]:<33} {r.indexed_files:>7,} {r.chunks:>9,} "
                f"{r.missing_chunks:>9,} {r.stale_chunks:>7,} {r.figures:>7,} "
                f"{r.missing_images:>9,} {r.unattached_images:>9,} {r.stale_images:>9,} "
                f"{r.empty_files:>6,}"
            )
    bad = [r for r in report.folders if not r.ok]
    print(
        f"\n{len(bad)} of {len(report.folders)} folder(s) need repair; "
        f"{sum(r.missing_chunks for r in bad):,} missing + "
        f"{sum(r.stale_chunks for r in bad):,} stale chunk vectors in "
        f"{sum(len(r.files_to_embed) for r in bad):,} file(s); "
        f"{sum(len(r.images_to_embed) for r in bad):,} image job(s); defunct points: "
        f"{len(report.defunct_chunk_points):,} chunk, {len(report.defunct_image_points):,} image"
    )
    empty = sum(r.empty_files for r in report.folders)
    if empty:
        print(f"{empty:,} indexed file(s) have no chunks and no figures (not repaired; Reindex them if unexpected)")


def _wait(job_ids: list[int]) -> None:
    pending = set(job_ids)
    last = 0.0
    while pending:
        with session_scope() as s:
            # Finished jobs, and jobs cleared from the table, both drop out.
            pending = {
                jid
                for jid, state in s.execute(
                    select(Job.id, Job.state).where(Job.id.in_(list(pending)))
                )
                if state in ("queued", "running")
            }
        if time.monotonic() - last > 30:
            print(f"  {len(job_ids) - len(pending):,} / {len(job_ids):,} repair jobs finished")
            last = time.monotonic()
        if pending:
            time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--folder", action="append", default=[], help="folder id or name (repeatable)")
    ap.add_argument("--repair", action="store_true", help="queue embed jobs and delete defunct points")
    ap.add_argument("--wait", action="store_true", help="with --repair: wait for the jobs, re-check, repeat")
    ap.add_argument("--passes", type=int, default=3, help="with --wait: at most this many passes")
    ap.add_argument("--allow-large-delete", action="store_true", help="delete defunct points past the 1%% bound")
    ap.add_argument(
        "--assume-empty",
        action="store_true",
        help="treat a collection that does not exist as lost (default: refuse, it is usually the "
        "wrong Qdrant); restart the app first so it recreates the collection",
    )
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="list healthy folders too")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    init_db()
    # The deployment's recorded profile decides which collections to read
    # (refuses on a mismatch, like the app's startup).
    profiles.resolve()
    folder_ids = _resolve_folders(args.folder)
    passes = args.passes if (args.repair and args.wait) else 1
    def _check() -> index_repair.IndexReport:
        with session_scope() as s:
            return index_repair.check(s, folder_ids, assume_empty=args.assume_empty)

    for n in range(1, passes + 1):
        try:
            report = _check()
        except index_repair.IndexUnreadable as e:
            print(f"cannot check: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(asdict(report) | {"ok": report.ok}, default=str))
        else:
            _print(report, verbose=args.verbose)
        if report.ok or not args.repair:
            return 0 if report.ok else 1
        try:
            with session_scope() as s:
                result = index_repair.repair(s, report, allow_large_delete=args.allow_large_delete)
        except index_repair.RepairRefused as e:
            print(f"repair refused: {e}", file=sys.stderr)
            return 2
        print(
            f"\npass {n}: queued {result.text_jobs:,} text + {result.image_jobs:,} image job(s); "
            f"deleted {result.deleted_chunk_points:,} chunk + {result.deleted_image_points:,} image point(s)"
        )
        if not args.wait:
            print("the app's workers run the jobs; run again without --repair to confirm")
            return 1
        _wait(result.job_ids)
    report = _check()
    _print(report, verbose=args.verbose)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
