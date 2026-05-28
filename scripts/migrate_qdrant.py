"""Migrate collections from the embedded (SQLite) Qdrant to a standalone Docker instance.

The embedded store is read-only during migration and left intact afterwards.
Switch VOITTA_QDRANT_MODE=standalone once you have verified the point counts.

Run::

    python -m scripts.migrate_qdrant --target-url http://192.168.88.212:6335

    # If the target already has data and you want to overwrite:
    python -m scripts.migrate_qdrant --target-url http://192.168.88.212:6335 --force
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from voitta_rag_enterprise.config import get_settings
from voitta_rag_enterprise.services.vector_store import CHUNKS, IMAGES

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # type: ignore[assignment]

BATCH_SIZE = 500


def _collection_exists(client: QdrantClient, name: str) -> bool:
    return any(c.name == name for c in client.get_collections().collections)


def _recreate_collection(
    src: QdrantClient, dst: QdrantClient, name: str, force: bool
) -> None:
    if _collection_exists(dst, name):
        info = dst.get_collection(name)
        if info.points_count and not force:
            print(
                f"\n  ERROR: target collection {name!r} already has "
                f"{info.points_count} points. Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
        dst.delete_collection(name)

    src_params = src.get_collection(name).config.params
    dst.create_collection(
        collection_name=name,
        vectors_config=src_params.vectors,
        sparse_vectors_config=src_params.sparse_vectors,
    )


def _migrate_collection(src: QdrantClient, dst: QdrantClient, name: str, total: int) -> int:
    offset: Any = None
    uploaded = 0

    if tqdm is not None:
        bar = tqdm(
            total=total,
            desc=f"  {name}",
            unit="pts",
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        )
    else:
        bar = None

    while True:
        results, next_offset = src.scroll(
            collection_name=name,
            limit=BATCH_SIZE,
            offset=offset,
            with_vectors=True,
            with_payload=True,
        )
        if not results:
            break

        points = [
            qm.PointStruct(id=r.id, vector=r.vector, payload=r.payload)
            for r in results
        ]
        dst.upsert(collection_name=name, points=points)
        uploaded += len(results)

        if bar is not None:
            bar.update(len(results))
        else:
            pct = int(uploaded / total * 100) if total else 0
            print(f"\r  {name}: {uploaded}/{total} ({pct}%)", end="", flush=True)

        if next_offset is None:
            break
        offset = next_offset

    if bar is not None:
        bar.close()
    else:
        print()

    return uploaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate embedded Qdrant → Docker")
    parser.add_argument(
        "--source-path",
        help="Path to embedded Qdrant store (default: from settings)",
    )
    parser.add_argument(
        "--target-url",
        help="Target Qdrant URL (default: VOITTA_QDRANT_URL from settings)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing collections on the target",
    )
    args = parser.parse_args()

    settings = get_settings()
    source_path = args.source_path or str(settings.resolved_qdrant_path())
    target_url = args.target_url or settings.qdrant_url

    if not target_url:
        print("ERROR: no target URL. Pass --target-url or set VOITTA_QDRANT_URL.", file=sys.stderr)
        sys.exit(1)

    print(f"Source : {source_path}")
    print(f"Target : {target_url}")
    print()

    src = QdrantClient(path=source_path)
    dst = QdrantClient(url=target_url)

    src_collections = {c.name for c in src.get_collections().collections}
    to_migrate = [n for n in (CHUNKS, IMAGES) if n in src_collections]

    if not to_migrate:
        print(f"No collections found in source ({source_path}). Nothing to do.")
        src.close()
        dst.close()
        return

    all_ok = True
    for name in to_migrate:
        src_count = src.get_collection(name).points_count or 0
        print(f"[{name}]  {src_count:,} points")
        _recreate_collection(src, dst, name, args.force)
        migrated = _migrate_collection(src, dst, name, src_count)
        dst_count = dst.get_collection(name).points_count or 0

        if dst_count == src_count:
            print(f"  ✓  {dst_count:,} points verified\n")
        else:
            print(f"  ✗  MISMATCH — source={src_count} migrated={migrated} target={dst_count}\n", file=sys.stderr)
            all_ok = False

    src.close()
    dst.close()

    if all_ok:
        print("Migration complete.")
        print()
        print("Next steps:")
        print("  1. Add to your .env:")
        print("       VOITTA_QDRANT_MODE=standalone")
        print(f"       VOITTA_QDRANT_URL={target_url}")
        print("  2. Restart the app.")
    else:
        print("Migration finished with errors — check output above.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
