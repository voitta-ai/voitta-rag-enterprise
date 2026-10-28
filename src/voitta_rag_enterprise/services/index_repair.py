"""Index consistency: compare SQL with Qdrant, then repair the difference.

SQL is the source of truth — it holds every chunk's text and every image's
CAS blob — and Qdrant holds the vectors built from them. When the two drift
(lost vectors, a model-version bump, rows deleted under a dead worker) search
silently misses content while the folder still says "indexed". This module
finds the drift id by id and repairs it with the app's own embed jobs, so
nothing is parsed again:

* **missing chunks** — a chunk of an indexed file has no point. Repair:
  ``embed_text`` for the file (it rebuilds the file's points from SQL).
* **stale chunks** — the point was built by another model version. Same
  repair.
* **missing images** — no point carries a figure's CAS blob. Repair:
  ``embed_image`` for one row per blob; the other rows sharing the blob
  attach to that point on the next pass.
* **unattached images** — the blob's point exists but does not list this
  file. Repair: ``embed_image`` for the row (it takes the attach path).
* **stale images** — the blob's point was built by another model version.
  Repair: delete the point, then embed the blob again.
* **defunct points** — a point whose chunk / image no longer exists in SQL.
  Repair: delete it, within the same bound the startup sweep respects.
* **empty files** — indexed, but no chunks and no figures. Reported only:
  that may be a genuinely empty file, and fixing it means a re-parse.

Repair jobs carry the file's current ``embed_round``, so a re-index that
starts meanwhile makes them abort instead of racing it, and they run below
normal priority. Only files in state ``indexed`` are considered: anything
else is still in flight or already shows as an error.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from qdrant_client.http import models as qm
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import Chunk, File, Folder, Image
from . import job_queue, vector_store

logger = logging.getLogger(__name__)

# Below routine work (0): a repair never delays fresh uploads.
REPAIR_PRIORITY = -10
_SCROLL_PAGE = 2048
# Same bound as the startup orphan sweep: deleting more than this share of
# a collection at once means SQL is probably not the database these vectors
# belong to.
_MAX_DELETE_SHARE = 0.01
_MIN_DELETE_CAP = 100


class RepairRefused(RuntimeError):
    """A repair would delete more points than a healthy index ever needs."""


@dataclass
class FolderReport:
    folder_id: int
    name: str
    indexed_files: int = 0
    chunks: int = 0
    missing_chunks: int = 0
    stale_chunks: int = 0
    figures: int = 0  # distinct image blobs
    missing_images: int = 0
    unattached_images: int = 0
    stale_images: int = 0
    empty_files: int = 0
    files_to_embed: list[tuple[int, int]] = field(default_factory=list)  # (file_id, round)
    images_to_embed: list[tuple[int, int, int]] = field(default_factory=list)  # (image_id, file_id, round)

    @property
    def ok(self) -> bool:
        return not (
            self.missing_chunks
            or self.stale_chunks
            or self.missing_images
            or self.unattached_images
            or self.stale_images
        )


@dataclass
class IndexReport:
    folders: list[FolderReport]
    sql_chunks: int
    chunk_points: int
    image_points: int
    defunct_chunk_points: list[int] = field(default_factory=list)
    defunct_image_points: list[int] = field(default_factory=list)
    stale_image_points: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.folders) and not (
            self.defunct_chunk_points or self.defunct_image_points
        )


@dataclass
class RepairResult:
    text_jobs: int = 0
    image_jobs: int = 0
    deleted_chunk_points: int = 0
    deleted_image_points: int = 0
    job_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Qdrant side
# ---------------------------------------------------------------------------


def _scroll(collection: str, keys: list[str]) -> list:
    """Every point's id + the named payload keys, one Qdrant call per page
    (the app's single Qdrant thread stays free for search in between)."""
    if not vector_store.run_on_qdrant(
        lambda: vector_store._collection_exists(vector_store.get_client(), collection)
    ):
        return []
    out: list = []
    offset = None
    while True:

        def _page(offset=offset):
            return vector_store.get_client().scroll(
                collection,
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=qm.PayloadSelectorInclude(include=keys),
                with_vectors=False,
            )

        page, offset = vector_store.run_on_qdrant(_page)
        out.extend(page)
        if offset is None:
            return out


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def check(db: Session, folder_ids: list[int] | None = None) -> IndexReport:
    """Compare SQL with Qdrant. Read-only."""
    settings = get_settings()
    folders = {
        f.id: f
        for f in db.execute(select(Folder)).scalars()
        if folder_ids is None or f.id in folder_ids
    }
    reports = {fid: FolderReport(fid, f.display_name) for fid, f in folders.items()}

    files = {
        fid: (folder_id, rnd)
        for fid, folder_id, rnd in db.execute(
            select(File.id, File.folder_id, File.embed_round).where(File.state == "indexed")
        )
        if folder_id in reports
    }
    for folder_id, _ in files.values():
        reports[folder_id].indexed_files += 1

    # Every chunk id in SQL (any file state: an in-flight file's points are
    # not defunct); which of them belong to settled files in scope.
    all_chunk_ids: set[int] = set()
    chunks_by_file: dict[int, list[int]] = defaultdict(list)
    for cid, file_id in db.execute(select(Chunk.id, Chunk.file_id)):
        all_chunk_ids.add(cid)
        if file_id in files:
            chunks_by_file[file_id].append(cid)

    all_cas: set[str] = set()
    figures_by_cas: dict[str, list[tuple[int, int]]] = defaultdict(list)  # cas -> [(image_id, file_id)]
    for iid, file_id, cas, kind in db.execute(
        select(Image.id, Image.file_id, Image.image_cas_id, Image.kind)
    ):
        all_cas.add(cas)
        if kind == "figure" and file_id in files:
            figures_by_cas[cas].append((iid, file_id))

    chunk_points = _scroll(
        vector_store.CHUNKS, ["kind", "dense_model_version", "sparse_model_version"]
    )
    image_points = _scroll(
        vector_store.IMAGES, ["image_cas_id", "file_ids", "image_model_version"]
    )
    report = IndexReport(
        folders=[],
        sql_chunks=len(all_chunk_ids),
        chunk_points=len(chunk_points),
        image_points=len(image_points),
    )

    point_versions: dict[int, tuple] = {}
    for p in chunk_points:
        pl = p.payload or {}
        if pl.get("kind") == vector_store.FOLDER_CARD_KIND:
            continue  # folder cards are rebuilt by their own job
        pid = int(p.id)
        if pid not in all_chunk_ids:
            report.defunct_chunk_points.append(pid)
            continue
        point_versions[pid] = (pl.get("dense_model_version"), pl.get("sparse_model_version"))

    want = (settings.dense_version, settings.sparse_version)
    for file_id, cids in chunks_by_file.items():
        folder_id, rnd = files[file_id]
        r = reports[folder_id]
        r.chunks += len(cids)
        missing = stale = 0
        for cid in cids:
            got = point_versions.get(cid)
            if got is None:
                missing += 1
            elif any(g and w and g != w for g, w in zip(got, want, strict=True)):
                stale += 1
        r.missing_chunks += missing
        r.stale_chunks += stale
        if missing or stale:
            r.files_to_embed.append((file_id, rnd))

    points_by_cas: dict[str, list] = defaultdict(list)
    for p in image_points:
        pl = p.payload or {}
        cas = pl.get("image_cas_id")
        if cas not in all_cas:
            report.defunct_image_points.append(int(p.id))
        else:
            points_by_cas[cas].append(p)

    files_with_figures: set[int] = set()
    for cas, rows in figures_by_cas.items():
        rows.sort()
        first_folder = files[rows[0][1]][0]
        reports[first_folder].figures += 1
        files_with_figures.update(f for _, f in rows)
        points = points_by_cas.get(cas, [])
        stale = [
            p
            for p in points
            if (v := (p.payload or {}).get("image_model_version"))
            and settings.image_version
            and v != settings.image_version
        ]
        if stale:
            report.stale_image_points.extend(int(p.id) for p in stale)
            points = [p for p in points if p not in stale]
        if not points:
            # No usable point: embed one row now; the rest attach next pass.
            iid, file_id = rows[0]
            r = reports[files[file_id][0]]
            if stale:
                r.stale_images += 1
            else:
                r.missing_images += 1
            r.images_to_embed.append((iid, file_id, files[file_id][1]))
            continue
        attached = {f for p in points for f in ((p.payload or {}).get("file_ids") or [])}
        seen: set[int] = set()
        for iid, file_id in rows:
            if file_id in attached or file_id in seen:
                continue
            seen.add(file_id)
            r = reports[files[file_id][0]]
            r.unattached_images += 1
            r.images_to_embed.append((iid, file_id, files[file_id][1]))

    for file_id, (folder_id, _) in files.items():
        if file_id not in chunks_by_file and file_id not in files_with_figures:
            reports[folder_id].empty_files += 1

    report.folders = sorted(reports.values(), key=lambda r: r.name.lower())
    if folder_ids is not None:
        # Defunct points have no folder in SQL; only a whole-index check
        # reports (and so repairs) them.
        report.defunct_chunk_points = []
        report.defunct_image_points = []
    return report


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def _delete_cap(total: int) -> int:
    return max(_MIN_DELETE_CAP, int(total * _MAX_DELETE_SHARE))


def repair(
    db: Session,
    report: IndexReport,
    *,
    allow_large_delete: bool = False,
) -> RepairResult:
    """Queue embed jobs for the drift in ``report`` and delete its defunct
    points. Commits through ``db``; the app's workers run the jobs."""
    result = RepairResult()

    defunct = [
        (vector_store.CHUNKS, report.defunct_chunk_points, report.chunk_points),
        (vector_store.IMAGES, report.defunct_image_points, report.image_points),
    ]
    for collection, ids, total in defunct:
        if not ids:
            continue
        if report.sql_chunks == 0 and collection == vector_store.CHUNKS:
            raise RepairRefused(
                f"SQL holds no chunks but {collection} has {total} points: this is "
                "not the database these vectors were built from; nothing deleted"
            )
        if len(ids) > _delete_cap(total) and not allow_large_delete:
            raise RepairRefused(
                f"{len(ids)} of {total} {collection} points have no row in SQL "
                f"(more than {_delete_cap(total)}); check that the app points at the "
                "right database, then pass --allow-large-delete"
            )
    for collection, ids, _ in defunct:
        if ids:
            _delete_points(collection, ids)
    result.deleted_chunk_points = len(report.defunct_chunk_points)
    result.deleted_image_points = len(report.defunct_image_points)

    if report.stale_image_points:
        # The blob is embedded again below; embed_image would otherwise
        # find the stale point and attach to it.
        _delete_points(vector_store.IMAGES, report.stale_image_points)

    for r in report.folders:
        for file_id, rnd in r.files_to_embed:
            result.job_ids.append(
                job_queue.enqueue(
                    db,
                    "embed_text",
                    {"file_id": file_id, "round": rnd},
                    dedup_key=f"embed_text:{file_id}",
                    priority=REPAIR_PRIORITY,
                )
            )
            result.text_jobs += 1
        for image_id, _file_id, rnd in r.images_to_embed:
            result.job_ids.append(
                job_queue.enqueue(
                    db,
                    "embed_image",
                    {"image_id": image_id, "round": rnd},
                    dedup_key=f"embed_image:{image_id}",
                    priority=REPAIR_PRIORITY,
                )
            )
            result.image_jobs += 1
    db.commit()
    logger.info(
        "index repair: queued %d embed_text + %d embed_image job(s); deleted %d "
        "defunct chunk + %d defunct image point(s)",
        result.text_jobs,
        result.image_jobs,
        result.deleted_chunk_points,
        result.deleted_image_points + len(report.stale_image_points),
    )
    return result


def _delete_points(collection: str, ids: list[int]) -> None:
    for i in range(0, len(ids), 1000):
        batch = ids[i : i + 1000]
        vector_store.run_on_qdrant(
            lambda batch=batch: vector_store.get_client().delete(
                collection, points_selector=qm.PointIdsList(points=batch), wait=True
            )
        )
