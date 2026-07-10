"""The ``delete_file`` job handler plus the shared ``wipe_file_data`` helper."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from ...db.database import session_scope
from ...db.models import Chunk, File, Image
from .. import events
from .common import _EXTRACT_LOCK


async def run_delete_file(payload: dict) -> None:
    file_id = int(payload["file_id"])
    await asyncio.to_thread(_delete_file_sync, file_id)


def wipe_file_data(file_id: int) -> None:
    """Delete every artifact associated with ``file_id`` *except* the file row.

    Wipes chunks, images, ChunkImageLinks, CAS refcounts, Qdrant chunk points,
    and removes the file_id from any shared image points (deleting the image
    point entirely if no other file references it).

    Acquires ``_EXTRACT_LOCK`` for the duration so we never race with an
    in-flight extract on the same file. Without that, an extract committing
    its own image-replacement mid-wipe leaves us deleting stale rows by id
    (the new rows extract just wrote stay alive — chunk/image counts in the
    UI never drop). Symptom was a wave of SAWarning "expected to delete N
    row(s); 0 were matched" alongside a reindex that visibly did nothing.

    Used by:
    * ``_delete_file_sync`` — file is gone; we then delete the file row.
    * ``reindex_folder`` — caller wants the file row to stay (state will be
      reset to ``pending``) but every downstream artifact must vanish so the
      stats counts reflect reality and stale Qdrant points don't leak into
      search results during the re-extract window.
    """
    from sqlalchemy import delete as sa_delete

    from ...cas import store as cas_store
    from .. import vector_store

    with _EXTRACT_LOCK, session_scope() as s:
        file = s.get(File, file_id)
        if file is None:
            return
        old_sha = file.file_cas_id
        # Use bulk DELETE statements rather than ORM s.delete() per row.
        # Bulk statements run under the writer lock and operate on the
        # current committed snapshot, so we can't end up with stale ORM
        # rows whose ids no longer exist in the DB. Also faster.

        # Decref CAS for each image before we drop the rows. Reading the
        # SHAs in one shot keeps the CAS bookkeeping consistent without
        # holding a list of ORM instances around the bulk DELETE.
        old_image_shas = [
            sha
            for (sha,) in s.execute(
                select(Image.image_cas_id).where(Image.file_id == file_id)
            ).all()
        ]
        for sha in old_image_shas:
            cas_store.decref(s, cas_store.KIND_IMAGE, sha)
        if old_sha is not None:
            cas_store.decref(s, cas_store.KIND_FILE, old_sha)

        # ChunkImageLink has CASCADE on both sides; deleting parents
        # removes the link rows automatically.
        s.execute(sa_delete(Image).where(Image.file_id == file_id))
        s.execute(sa_delete(Chunk).where(Chunk.file_id == file_id))

    vector_store.delete_chunks_for_file(file_id)
    vector_store.remove_file_from_image_points(file_id)


def _delete_file_sync(file_id: int) -> None:
    """Worker handler: delete the file row plus every artifact under it."""
    from ..folder_stats import publish_folder_stats

    # Capture folder_id before deleting so the post-delete stats publish
    # can find the folder row.
    folder_id: int | None = None
    wipe_file_data(file_id)
    with session_scope() as s:
        file = s.get(File, file_id)
        if file is not None:
            folder_id = file.folder_id
            s.delete(file)
    events.publish(
        "files",
        {"type": "file.deleted", "file_id": file_id, "folder_id": folder_id},
    )
    if folder_id is not None:
        with session_scope() as s:
            publish_folder_stats(s, folder_id)
