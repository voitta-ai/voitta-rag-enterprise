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
import logging
import sys
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from voitta_rag_enterprise.config import get_settings
from voitta_rag_enterprise.services.vector_store import CHUNKS, IMAGES

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def _collection_exists(client: QdrantClient, name: str) -> bool:
    return any(c.name == name for c in client.get_collections().collections)


def _recreate_collection(
    src: QdrantClient, dst: QdrantClient, name: str, force: bool
) -> None:
    if _collection_exists(dst, name):
        info = dst.get_collection(name)
        if info.points_count and not force:
            logger.error(
                "Target collection %r already has %d points. "
                "Use --force to overwrite.",
                name,
                info.points_count,
            )
            sys.exit(1)
        dst.delete_collection(name)
        logger.info("dropped existing target collection %r", name)

    src_info = src.get_collection(name)
    src_params = src_info.config.params

    dst.create_collection(
        collection_name=name,
        vectors_config=src_params.vectors,
        sparse_vectors_config=src_params.sparse_vectors,
    )
    logger.info("created target collection %r", name)


def _migrate_collection(src: QdrantClient, dst: QdrantClient, name: str) -> int:
    offset: Any = None
    total = 0

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

        dst.upsert(
            collection_name=name,
            points=results,
        )
        total += len(results)
        logger.info("  %s: %d points uploaded", name, total)

        if next_offset is None:
            break
        offset = next_offset

    return total


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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    source_path = args.source_path or str(settings.resolved_qdrant_path())
    target_url = args.target_url or settings.qdrant_url

    if not target_url:
        logger.error(
            "No target URL. Pass --target-url or set VOITTA_QDRANT_URL."
        )
        sys.exit(1)

    logger.info("Source: %s", source_path)
    logger.info("Target: %s", target_url)

    src = QdrantClient(path=source_path)
    dst = QdrantClient(url=target_url)

    src_collections = {c.name for c in src.get_collections().collections}
    to_migrate = [n for n in (CHUNKS, IMAGES) if n in src_collections]

    if not to_migrate:
        logger.warning("No collections found in source (%s). Nothing to do.", source_path)
        src.close()
        dst.close()
        return

    for name in to_migrate:
        src_count = src.get_collection(name).points_count
        logger.info("Migrating %r (%d points) ...", name, src_count)
        _recreate_collection(src, dst, name, args.force)
        migrated = _migrate_collection(src, dst, name)
        dst_count = dst.get_collection(name).points_count
        status = "OK" if dst_count == src_count else "MISMATCH"
        logger.info(
            "%s: %r — source=%d migrated=%d target=%d [%s]",
            status, name, src_count, migrated, dst_count, status,
        )
        if status != "OK":
            logger.warning(
                "Point count mismatch for %r — check for errors above.", name
            )

    src.close()
    dst.close()
    logger.info("Done. Set VOITTA_QDRANT_MODE=standalone and restart the app.")


if __name__ == "__main__":
    main()
