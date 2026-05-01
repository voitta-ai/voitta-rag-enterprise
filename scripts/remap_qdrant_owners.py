"""One-shot: rewrite ``allowed_users`` on Qdrant points after a SQLite-side
ACL remap.

Background. ``allowed_users`` is stamped onto every chunk / image point
*at index time* from the folder's then-current ACL. If the folder owner
changes after indexing, in-memory queries (which filter by
``allowed_users``) won't return those points to the new owner unless we
also rewrite the payloads. The architecture flagged this as "deferred"
and assumed a full re-index would be the answer; this script lets us
patch in place without re-embedding.

The Qdrant local store holds an exclusive file lock, so the app must be
stopped before running. Usage::

    # Inspect — no changes
    python scripts/remap_qdrant_owners.py --qdrant-path /mnt/.../qdrant

    # Apply — for every point whose allowed_users contains FROM_ID, write
    # back the same payload with FROM_ID replaced by TO_ID.
    python scripts/remap_qdrant_owners.py \
        --qdrant-path /mnt/.../qdrant --from 3 --to 5 --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("remap")

# Both collections we maintain. Keep in sync with vector_store.CHUNKS / IMAGES.
COLLECTIONS = ["chunks", "images"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--qdrant-path",
        type=Path,
        required=True,
        help="Path to the Qdrant local-mode store directory.",
    )
    ap.add_argument(
        "--from",
        dest="from_id",
        type=int,
        help="Old user id to replace.",
    )
    ap.add_argument(
        "--to",
        dest="to_id",
        type=int,
        help="New user id to install.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually rewrite payloads. Without this flag the script just inspects.",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Scroll page size; lower if memory is tight.",
    )
    args = ap.parse_args()

    if args.apply and (args.from_id is None or args.to_id is None):
        ap.error("--apply requires --from and --to")

    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm  # noqa: F401  (re-exported below if used)

    client = QdrantClient(path=str(args.qdrant_path))
    try:
        any_changes = False
        for col in COLLECTIONS:
            if not client.collection_exists(col):
                logger.info("[%s] collection missing — skipping", col)
                continue
            info = client.get_collection(col)
            logger.info("[%s] points=%d", col, info.points_count)

            # First pass: histogram of (folder_id, allowed_users) so we can see
            # what's in there before touching anything.
            histogram: Counter[tuple[int | None, tuple[int, ...]]] = Counter()
            offset = None
            scanned = 0
            while True:
                points, offset = client.scroll(
                    col,
                    limit=args.batch_size,
                    with_payload=True,
                    offset=offset,
                )
                if not points:
                    break
                for p in points:
                    payload = p.payload or {}
                    fid = payload.get("folder_id")
                    au = tuple(sorted(payload.get("allowed_users") or []))
                    histogram[(fid, au)] += 1
                scanned += len(points)
                if offset is None:
                    break
            logger.info(
                "[%s] scanned=%d distinct (folder_id, allowed_users) tuples=%d",
                col,
                scanned,
                len(histogram),
            )
            for (fid, au), n in sorted(histogram.items(), key=lambda kv: -kv[1]):
                logger.info(
                    "    folder_id=%s allowed_users=%s  →  %d points",
                    fid,
                    list(au),
                    n,
                )

            if not args.apply:
                continue

            # Second pass: rewrite points that contain ``from_id``.
            # ``set_payload`` on a single key is partial — replace ONLY
            # ``allowed_users`` while leaving everything else (file_id,
            # text, model versions, etc.) untouched.
            offset = None
            updated = 0
            while True:
                points, offset = client.scroll(
                    col,
                    limit=args.batch_size,
                    with_payload=True,
                    offset=offset,
                )
                if not points:
                    break
                for p in points:
                    payload = p.payload or {}
                    au = list(payload.get("allowed_users") or [])
                    if args.from_id not in au:
                        continue
                    new_au = [args.to_id if u == args.from_id else u for u in au]
                    # Defensive: if both ids are present we'd duplicate. Dedup.
                    seen: set[int] = set()
                    deduped: list[int] = []
                    for u in new_au:
                        if u in seen:
                            continue
                        seen.add(u)
                        deduped.append(u)
                    client.set_payload(
                        col,
                        payload={"allowed_users": deduped},
                        points=[p.id],
                    )
                    updated += 1
                if offset is None:
                    break
            if updated:
                any_changes = True
            logger.info("[%s] rewrote allowed_users on %d point(s)", col, updated)
        if args.apply and not any_changes:
            logger.info("apply requested but nothing matched — no writes performed")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
