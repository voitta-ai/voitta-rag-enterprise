"""Re-enqueue embed jobs for points whose model_version doesn't match config.

Walks the Qdrant ``chunks`` and ``images`` collections looking for points whose
``dense_model_version`` / ``sparse_model_version`` / ``image_model_version``
payload differs from the configured value, and re-enqueues ``embed_text`` /
``embed_image`` jobs for the corresponding files / images.

Run after bumping ``VOITTA_DENSE_VERSION`` / ``VOITTA_SPARSE_VERSION`` /
``VOITTA_IMAGE_VERSION`` (or after switching the model name itself)::

    python -m scripts.reembed_stale
"""

from __future__ import annotations

import argparse
import logging

from voitta_rag_enterprise.config import get_settings
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.services import job_queue, vector_store

logger = logging.getLogger(__name__)


def _scan_for_stale(collection: str, version_keys: list[str], expected: dict[str, str]) -> set[tuple[str, int]]:
    """Return ``(payload_key, target_id)`` pairs to re-embed."""

    def _do() -> list:
        client = vector_store.get_client()
        try:
            client.get_collection(collection)
        except Exception:
            return []
        offset = None
        out: list = []
        while True:
            res, offset = client.scroll(
                collection,
                limit=1000,
                offset=offset,
                with_payload=True,
            )
            out.extend(res)
            if offset is None:
                break
        return out

    points = vector_store.run_on_qdrant(_do)
    stale: set[tuple[str, int]] = set()
    for p in points:
        payload = dict(p.payload or {})
        for key in version_keys:
            if expected[key] and payload.get(key) and payload[key] != expected[key]:
                if collection == vector_store.CHUNKS:
                    stale.add(("file", int(payload["file_id"])))
                else:
                    stale.add(("image", int(payload["image_id"])))
                break
    return stale


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    init_db()

    stale_text = _scan_for_stale(
        vector_store.CHUNKS,
        ["dense_model_version", "sparse_model_version"],
        {
            "dense_model_version": settings.dense_version,
            "sparse_model_version": settings.sparse_version,
        },
    )
    stale_images = _scan_for_stale(
        vector_store.IMAGES,
        ["image_model_version"],
        {"image_model_version": settings.image_version},
    )

    text_files = {tid for kind, tid in stale_text if kind == "file"}
    image_ids = {tid for kind, tid in stale_images if kind == "image"}

    logger.info(
        "stale: %d file(s) for embed_text, %d image point(s) for embed_image",
        len(text_files),
        len(image_ids),
    )
    if args.dry_run:
        return

    with session_scope() as s:
        for fid in sorted(text_files):
            job_queue.enqueue(
                s, "embed_text", {"file_id": fid}, dedup_key=f"embed_text:{fid}"
            )
        for iid in sorted(image_ids):
            job_queue.enqueue(
                s, "embed_image", {"image_id": iid}, dedup_key=f"embed_image:{iid}"
            )
    logger.info("re-embed jobs enqueued")


if __name__ == "__main__":
    main()
