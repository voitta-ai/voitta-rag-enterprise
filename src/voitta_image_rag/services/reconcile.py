"""Cross-store health check.

The system has two stores that must agree:

- SQLite ``files`` rows whose ``state == 'indexed'`` represent files that
  *should* have chunk points in Qdrant.
- Qdrant ``chunks`` collection holds the actual searchable points.

If the Qdrant data dir is wiped or the path is changed, SQLite still says
"indexed" and the SPA reports "X / Y indexed", but search returns nothing
because the points aren't there. This module reports that mismatch so
it's surfaced — currently in the doctor CLI, the app's startup logs, and
the per-folder REST stats response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import File, Folder
from . import vector_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FolderHealth:
    folder_id: int
    display_name: str
    indexed_files: int  # count(state='indexed') in SQLite
    qdrant_chunk_points: int  # count of chunk points where payload.folder_id == folder_id
    status: str  # "ok" | "empty" | "out_of_sync"

    @property
    def out_of_sync(self) -> bool:
        return self.status == "out_of_sync"


def _classify(indexed_files: int, points: int) -> str:
    """Decide the status bucket for a folder.

    - ``empty``: nothing indexed in SQLite → no expectation of Qdrant points.
    - ``out_of_sync``: SQLite says some files are indexed but Qdrant has
      zero matching points. Concretely: Qdrant data dir was wiped or
      changed, app needs a Reindex per folder to repopulate.
    - ``ok``: both stores agree, *or* Qdrant has points (we don't enforce a
      strict ratio because background jobs may still be embedding —
      "indexed but no points" is the only state worth panicking about).
    """
    if indexed_files == 0:
        return "empty"
    if points == 0:
        return "out_of_sync"
    return "ok"


def folder_health(db: Session, folder: Folder) -> FolderHealth:
    """Compute the index-health status for one folder.

    Cheap: one ``count(*)`` query against SQLite plus one Qdrant
    ``count`` (which uses the inverted index on ``folder_id``).
    """
    indexed = db.execute(
        select(func.count(File.id)).where(
            File.folder_id == folder.id, File.state == "indexed"
        )
    ).scalar_one() or 0
    points = vector_store.count_points_for_folder(
        vector_store.CHUNKS, folder.id
    )
    return FolderHealth(
        folder_id=folder.id,
        display_name=folder.display_name,
        indexed_files=int(indexed),
        qdrant_chunk_points=int(points),
        status=_classify(int(indexed), int(points)),
    )


def all_folder_health(db: Session) -> list[FolderHealth]:
    """Return health for every folder, ordered by id."""
    folders = list(db.execute(select(Folder).order_by(Folder.id)).scalars())
    return [folder_health(db, f) for f in folders]


def log_startup_warnings(db: Session) -> None:
    """Run during app lifespan startup; emit one WARNING per out-of-sync folder.

    Intentionally non-fatal — the app still boots and serves what it can.
    Visibility lives in ``~/.voitta-image-rag/logs/app.log`` and on the SPA
    folder-detail panel via the ``index_health`` field on ``GET /folders/{id}/stats``.
    """
    out_of_sync = [h for h in all_folder_health(db) if h.out_of_sync]
    if not out_of_sync:
        return
    for h in out_of_sync:
        logger.warning(
            "index health: folder %d %r has %d file(s) marked indexed in SQLite "
            "but 0 chunk points in Qdrant — Reindex this folder to repopulate "
            "the vector store.",
            h.folder_id,
            h.display_name,
            h.indexed_files,
        )
