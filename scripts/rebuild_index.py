"""Reset every derived artefact and re-enqueue extraction for every file.

Drops:
- Qdrant ``chunks`` and ``images`` collections
- ``cas/files`` and ``cas/images`` directories
- SQLite ``chunks``, ``images``, ``chunk_image_links``, ``cas_refs`` rows

Keeps:
- ``users``, ``folders``, ``folder_acl``, ``file_acl``
- ``files`` rows (rel_path, ACL, mtime); resets state to ``pending`` and clears
  ``file_cas_id``/``last_indexed_at``/``error``/``pending_embeds``.

Run::

    python -m scripts.rebuild_index --yes
"""

from __future__ import annotations

import argparse
import logging
import shutil

from sqlalchemy import delete, update

from voitta_image_rag.cas import store as cas_store
from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import (
    CasRef,
    Chunk,
    ChunkImageLink,
    File,
    Image,
)
from voitta_image_rag.services import job_queue, vector_store

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.yes:
        confirm = input("Drop CAS + Qdrant and rebuild every file? [yes/N] ")
        if confirm.strip().lower() not in ("y", "yes"):
            logger.info("aborted")
            return

    init_db()

    files_dir = cas_store.files_dir()
    images_dir = cas_store.images_dir()
    if files_dir.exists():
        shutil.rmtree(files_dir)
        logger.info("removed %s", files_dir)
    if images_dir.exists():
        shutil.rmtree(images_dir)
        logger.info("removed %s", images_dir)

    def _drop_qdrant() -> None:
        client = vector_store.get_client()
        for name in (vector_store.CHUNKS, vector_store.IMAGES):
            try:
                client.delete_collection(name)
                logger.info("dropped qdrant collection: %s", name)
            except Exception as e:  # collection may not exist
                logger.debug("delete_collection(%s) skipped: %s", name, e)

    vector_store.run_on_qdrant(_drop_qdrant)

    enqueued = 0
    with session_scope() as s:
        s.execute(delete(ChunkImageLink))
        s.execute(delete(Chunk))
        s.execute(delete(Image))
        s.execute(delete(CasRef))
        s.execute(
            update(File).values(
                file_cas_id=None,
                state="pending",
                pending_embeds=0,
                last_indexed_at=None,
                error=None,
            )
        )
        for f in s.execute(File.__table__.select()).all():
            file_id = f.id
            job_queue.enqueue(
                s, "extract", {"file_id": file_id}, dedup_key=f"extract:{file_id}"
            )
            enqueued += 1

    logger.info("enqueued %d extract job(s); workers will pick them up", enqueued)


if __name__ == "__main__":
    main()
