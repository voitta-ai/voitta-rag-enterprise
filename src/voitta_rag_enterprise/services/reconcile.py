"""Cross-store health check.

The system has two stores that must agree:

- SQLite ``files`` rows whose ``state == 'indexed'`` represent files whose
  chunks and figures *should* all have points in Qdrant.
- Qdrant's chunks and images collections hold the actual searchable points.

If the Qdrant data dir is wiped, moved or partly deleted, SQLite still says
"indexed" and the SPA reports "X / Y indexed", but search silently skips
every chunk whose point is gone. This module reports that mismatch so it's
surfaced — in the doctor CLI, the app's startup logs, and the per-folder
REST stats response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Chunk, File, Folder, Image
from . import vector_store

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class FolderHealth:
    folder_id: int
    display_name: str
    indexed_files: int  # count(state='indexed') in SQLite
    expected_chunk_points: int  # SQL chunks of those indexed files
    qdrant_chunk_points: int  # chunk points where payload.folder_id == folder_id (no cards)
    status: str  # "ok" | "empty" | "out_of_sync"

    @property
    def out_of_sync(self) -> bool:
        return self.status == "out_of_sync"

    @property
    def missing_chunk_points(self) -> int:
        return max(0, self.expected_chunk_points - self.qdrant_chunk_points)

@dataclass(frozen=True)
class ImageHealth:
    """Figures are deduplicated by content into one point per image across
    all folders, so their check is install-wide rather than per folder."""

    expected_image_points: int  # distinct figure images of indexed files
    qdrant_image_points: int

    @property
    def missing_image_points(self) -> int:
        return max(0, self.expected_image_points - self.qdrant_image_points)

def _classify(indexed_files: int, expected: int, points: int) -> str:
    """Decide the status bucket for a folder.

    - ``empty``: nothing indexed in SQLite → no expectation of Qdrant points.
    - ``out_of_sync``: fewer chunk points than the indexed files have
      chunks. An indexed file's points are written before it becomes
      ``indexed`` and deleted only after its chunk rows, so in a healthy
      install every one is there; a gap means Qdrant lost points (wiped,
      moved or partly deleted) and the folder needs a Reindex.
    - ``ok``: every chunk of an indexed file has its point. Points of files
      still being embedded may push the count above the expectation; that
      is never a problem.
    """
    if indexed_files == 0:
        return "empty"
    if points < expected:
        return "out_of_sync"
    return "ok"

def folder_health(db: Session, folder: Folder) -> FolderHealth:
    """Compute the index-health status for one folder.

    Cheap: two ``count(*)`` queries against SQLite plus one Qdrant
    ``count`` (which uses the payload index on ``folder_id``).
    """
    indexed_file = (File.folder_id == folder.id) & (File.state == "indexed")
    indexed = db.execute(select(func.count(File.id)).where(indexed_file)).scalar_one() or 0
    expected = db.execute(
        select(func.count(Chunk.id)).join(File, Chunk.file_id == File.id).where(indexed_file)
    ).scalar_one() or 0
    points = vector_store.count_points_for_folder(
        vector_store.chunks_collection(), folder.id
    )
    return FolderHealth(
        folder_id=folder.id,
        display_name=folder.display_name,
        indexed_files=int(indexed),
        expected_chunk_points=int(expected),
        qdrant_chunk_points=int(points),
        status=_classify(int(indexed), int(expected), int(points)),
    )

def all_folder_health(db: Session) -> list[FolderHealth]:
    """Return health for every folder, ordered by id."""
    folders = list(db.execute(select(Folder).order_by(Folder.id)).scalars())
    return [folder_health(db, f) for f in folders]

def image_health(db: Session) -> ImageHealth:
    """Install-wide figure check: one point per distinct figure image of an
    indexed file (page renders are never embedded)."""
    expected = db.execute(
        select(func.count(func.distinct(Image.image_cas_id)))
        .join(File, Image.file_id == File.id)
        .where(Image.kind == "figure", File.state == "indexed")
    ).scalar_one() or 0
    collection = vector_store.images_collection()
    points = vector_store.collection_point_counts([collection])[collection]
    return ImageHealth(expected_image_points=int(expected), qdrant_image_points=int(points))

def log_startup_warnings(db: Session) -> None:
    """Run during app lifespan startup; emit one WARNING per out-of-sync
    folder, and one for missing figure points.

    Intentionally non-fatal — the app still boots and serves what it can.
    Visibility lives in ``$VOITTA_DATA_DIR/logs/app.log`` and on the SPA
    folder-detail panel via the ``index_health`` field on ``GET /folders/{id}/stats``.
    """
    for h in all_folder_health(db):
        if h.out_of_sync:
            logger.warning(
                "index health: folder %d %r: %d of %d chunk(s) of its %d indexed "
                "file(s) have no point in Qdrant, so search cannot find them — "
                "Reindex this folder to repopulate the vector store.",
                h.folder_id,
                h.display_name,
                h.missing_chunk_points,
                h.expected_chunk_points,
                h.indexed_files,
            )
    ih = image_health(db)
    if ih.missing_image_points:
        logger.warning(
            "index health: %d of %d figure image(s) of indexed files have no point "
            "in Qdrant, so image search cannot find them — Reindex the affected folders.",
            ih.missing_image_points,
            ih.expected_image_points,
        )
