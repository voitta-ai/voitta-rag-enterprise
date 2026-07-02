"""Startup recovery — cross-check jobs ↔ file rows ↔ disk and repair drift.

A killed process (SIGKILL mid-extract, mid-delete, mid-upload burst) leaves
the three stores this app spans — the jobs table, the files table, and the
actual filesystem — telling different stories. The existing sweeps each fix
one axis:

* ``job_queue.reclaim_abandoned_jobs`` — zombie ``running`` job rows.
* ``indexing.reconcile_abandoned_extracts`` — files stuck mid-pipeline
  (``extracted``/``embedding``) and stranded ``pending`` rows.
* the Qdrant orphan-point sweep in ``main._finish_startup``.

What none of them do is consult the **disk**. The failure mode this module
exists for (observed in the field): a folder scan raced an upload burst,
files that were present on disk got flagged ``state='deleted'`` with
``delete_file`` jobs enqueued — then the process was killed. On restart the
surviving queued ``delete_file`` jobs run *before* any freshly-enqueued
extracts (lower job ids win) and remove rows for files that are sitting on
disk, so a restart makes things worse, not better.

``run_startup_recovery`` runs once, from the lifespan's background startup:

    reclaim_abandoned_jobs()      # settle zombie 'running' rows first
    run_startup_recovery()        # <-- this module
    folder_active.init_from_db()  # recount AFTER we cancel/enqueue jobs
    reconcile_abandoned_extracts()

Ordering matters: we cancel and enqueue jobs, so the folder-active bootstrap
must run after us, and the zombie sweep must run before us (``queued`` is the
only live pre-worker job state we have to reason about).

Two invariants everything below follows:

* **Never repair on evidence we can't read.** Disk checks answer
  True/False/None; ``None`` (root missing, cloud mount offline, folder
  disabled) means hands off — a dead NFS mount must not become a mass delete,
  nor a mass resurrect.
* **No events are published here.** Every repaired row is handed a follow-up
  job (extract or delete_file) whose completion publishes the authoritative
  ``file.upserted`` / ``file.deleted`` through the existing single-source-of-
  truth publishers. Recovery only rewrites state; the pipeline announces it.

Everything here is a no-op on a healthy database.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db.models import Chunk, File, Folder, Image, Job
from . import job_queue
from .ignore import IgnoreMatcher
from .ignore import from_settings as _ignore_from_settings
from .scanner import file_present_in_scope, resolve_scan_roots

logger = logging.getLogger(__name__)

_FILE_JOB_KINDS = ("extract", "embed_text", "embed_image", "delete_file")
_FOLDER_JOB_KINDS = ("sync", "reindex_folder")

# ``File.error`` prefixes that indicate an infrastructure failure (I/O error,
# crashed worker, interrupted embed) rather than a deterministic parser
# rejection. Only these are retried at startup — a file the parser genuinely
# cannot handle would fail identically and burn a full parse on every restart.
_RETRYABLE_ERROR_PREFIXES = (
    "read failed",
    "stat failed",
    "extract crashed",
    "post-parse pipeline failed",
    "inline text embed failed",
    "embed_text failed",
    "embed_image failed",
)


@dataclass
class RecoveryReport:
    """Counters for everything the sweep changed; logged at the end."""

    cancelled_orphan_jobs: int = 0    # queued jobs pointing at missing rows
    cancelled_delete_jobs: int = 0    # delete_file jobs whose target is on disk
    resurrected_files: int = 0        # deleted/inconsistent rows -> pending
    completed_deletes: int = 0        # deleted rows with no job -> delete re-enqueued
    snapped_counters: int = 0         # indexed rows with pending_embeds > 0
    retried_errors: int = 0           # error rows with retryable errors -> pending

    def total(self) -> int:
        return (
            self.cancelled_orphan_jobs
            + self.cancelled_delete_jobs
            + self.resurrected_files
            + self.completed_deletes
            + self.snapped_counters
            + self.retried_errors
        )


@dataclass
class _Scope:
    """A folder's disk scope: root plus the subtrees a scan would walk.

    ``scan_roots is None`` means the disk state is unknowable right now
    (missing root, offline cloud mount) or out of bounds (folder disabled) —
    every presence probe answers ``None`` and the sweeps skip the folder.
    """

    root: Path
    scan_roots: list[Path] | None


def _load_scopes(session: Session) -> dict[int, _Scope]:
    scopes: dict[int, _Scope] = {}
    for folder in session.execute(select(Folder)).scalars():
        # Disabled folders sit outside the scanner's and watcher's scope;
        # recovery must not re-initiate work for them either.
        roots = resolve_scan_roots(session, folder) if folder.enabled else None
        scopes[folder.id] = _Scope(root=Path(folder.path), scan_roots=roots)
    return scopes


def _present(
    scopes: dict[int, _Scope],
    ignore: IgnoreMatcher,
    folder_id: int,
    rel_path: str,
) -> bool | None:
    """Is this file really on disk? ``None`` = can't tell, hands off."""
    scope = scopes.get(folder_id)
    if scope is None or scope.scan_roots is None:
        return None
    return file_present_in_scope(scope.root, scope.scan_roots, ignore, rel_path)


def _cancel_job(job: Job, reason: str) -> None:
    job.state = "done"
    job.error = f"cancelled by startup recovery: {reason}"
    job.finished_at = int(time.time())


def _resurrect(session: Session, file: File) -> None:
    """Put a row back into the pipeline: ``state='pending'`` + a fresh extract.

    ``pending_embeds`` is deliberately preserved: a positive count means embed
    work was lost (e.g. we are retrying a failed inline embed whose chunks are
    committed but whose Qdrant points never landed), and
    ``_short_circuit_unchanged`` reads pending>0 as "must fully re-extract".
    Zeroing it would let an unchanged sha heal the row straight to 'indexed'
    with its vectors missing.

    If the chunks are gone while a CAS pointer remains, an interrupted
    ``delete_file`` wiped the artifacts after this row was flagged — drop the
    pointer so the sha short-circuit can't declare an empty index 'indexed'.
    No decref: that wipe already decref'd this sha, and a double decref could
    free a blob another file still shares. (A legitimately empty file also
    lands here and over-retains one refcount on its tiny blob — the safe
    direction.)
    """
    has_chunks = (
        session.execute(
            select(Chunk.id).where(Chunk.file_id == file.id).limit(1)
        ).first()
        is not None
    )
    if not has_chunks and file.file_cas_id is not None:
        file.file_cas_id = None
    file.state = "pending"
    file.error = None
    # Any straggler embed job from before the restart must take the
    # stale-round path rather than corrupt the fresh pipeline's counter.
    file.embed_round = (file.embed_round or 0) + 1
    job_queue.enqueue(
        session, "extract", {"file_id": file.id}, dedup_key=f"extract:{file.id}"
    )


def _sweep_jobs(
    session: Session,
    scopes: dict[int, _Scope],
    ignore: IgnoreMatcher,
    report: RecoveryReport,
) -> set[int]:
    """Phase A: queued-job hygiene.

    Cancels queued jobs whose target row no longer exists, and — the
    restart-makes-it-worse fix — cancels ``delete_file`` jobs whose target
    file is demonstrably present on disk, resurrecting the row instead.

    Returns the file_ids that still have a live ``delete_file`` job after the
    sweep, so phase B leaves those rows to the worker.
    """
    live_deletes: set[int] = set()
    jobs = list(
        session.execute(
            select(Job).where(
                Job.state == "queued",
                Job.kind.in_(_FILE_JOB_KINDS + _FOLDER_JOB_KINDS),
            )
        ).scalars()
    )
    for job in jobs:
        try:
            payload = json.loads(job.payload) if job.payload else {}
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            _cancel_job(job, "unparseable payload")
            report.cancelled_orphan_jobs += 1
            continue

        if job.kind in _FOLDER_JOB_KINDS:
            fid = payload.get("folder_id")
            if not isinstance(fid, int) or session.get(Folder, fid) is None:
                _cancel_job(job, "folder row gone")
                report.cancelled_orphan_jobs += 1
            continue

        # File-scoped kinds. embed_image is keyed by image_id — resolve it.
        file_id = payload.get("file_id")
        if job.kind == "embed_image" and not isinstance(file_id, int):
            image_id = payload.get("image_id")
            row = (
                session.execute(
                    select(Image.file_id).where(Image.id == image_id)
                ).first()
                if isinstance(image_id, int)
                else None
            )
            file_id = row[0] if row is not None else None
        file = session.get(File, file_id) if isinstance(file_id, int) else None
        if file is None:
            _cancel_job(job, "file row gone")
            report.cancelled_orphan_jobs += 1
            continue

        if job.kind == "delete_file":
            if _present(scopes, ignore, file.folder_id, file.rel_path) is True:
                # The bytes are right there — this delete is a leftover from
                # a scan/watcher race, not a user intent we can still trust.
                _cancel_job(job, "target file present on disk")
                report.cancelled_delete_jobs += 1
                _resurrect(session, file)
                report.resurrected_files += 1
            else:
                live_deletes.add(file.id)
    return live_deletes


def _sweep_files(
    session: Session,
    scopes: dict[int, _Scope],
    ignore: IgnoreMatcher,
    live_deletes: set[int],
    report: RecoveryReport,
) -> None:
    """Phase B: file rows whose state disagrees with disk or with itself.

    ``pending``/``extracted``/``embedding`` rows are deliberately not touched
    here — ``reconcile_abandoned_extracts`` (which runs right after us) owns
    those.
    """
    # B1: rows flagged deleted with no live delete job — killed mid-delete
    # (job reclaimed to 'error', never retried) or the delete enqueue died
    # with the queue. Present on disk -> resurrect; absent -> finish the
    # interrupted delete; unknown -> leave alone.
    for file in session.execute(
        select(File).where(File.state == "deleted")
    ).scalars():
        if file.id in live_deletes:
            continue
        on_disk = _present(scopes, ignore, file.folder_id, file.rel_path)
        if on_disk is True:
            _resurrect(session, file)
            report.resurrected_files += 1
        elif on_disk is False:
            job_queue.enqueue(
                session,
                "delete_file",
                {"file_id": file.id},
                dedup_key=f"delete:{file.id}",
            )
            report.completed_deletes += 1

    # B2: counter drift — 'indexed' means every embed completed, so a
    # positive pending_embeds is a lost decrement that makes the UI count
    # the file as forever mid-embed.
    res = session.execute(
        text(
            "UPDATE files SET pending_embeds = 0 "
            "WHERE state = 'indexed' AND pending_embeds > 0"
        )
    )
    report.snapped_counters += res.rowcount

    # B3: 'indexed' with no CAS pointer has nothing behind the label — it
    # can't serve get_file / chunk reads and can't sha-short-circuit. Re-run.
    for file in session.execute(
        select(File).where(File.state == "indexed", File.file_cas_id.is_(None))
    ).scalars():
        _resurrect(session, file)
        report.resurrected_files += 1

    # B4: retry infrastructure-shaped errors once per startup. The file must
    # be verifiably on disk; parser rejections are left for the user (they
    # would fail identically every boot).
    for file in session.execute(
        select(File).where(File.state == "error")
    ).scalars():
        if not (file.error or "").startswith(_RETRYABLE_ERROR_PREFIXES):
            continue
        if _present(scopes, ignore, file.folder_id, file.rel_path) is not True:
            continue
        _resurrect(session, file)
        report.retried_errors += 1


def run_startup_recovery() -> RecoveryReport:
    """Run the full sweep. Returns the report (all zeros on a healthy DB)."""
    from ..db.database import session_scope

    report = RecoveryReport()
    ignore = _ignore_from_settings()
    with session_scope() as session:
        scopes = _load_scopes(session)
        unreachable = sorted(
            fid for fid, s in scopes.items() if s.scan_roots is None
        )
        if unreachable:
            logger.warning(
                "startup recovery: disk state unknown for folder(s) %s — "
                "leaving their rows untouched",
                unreachable,
            )
        live_deletes = _sweep_jobs(session, scopes, ignore, report)
        _sweep_files(session, scopes, ignore, live_deletes, report)

    if report.total():
        logger.warning(
            "startup recovery repaired %d item(s): "
            "orphan_jobs=%d delete_jobs_cancelled=%d resurrected=%d "
            "deletes_completed=%d counters_snapped=%d errors_retried=%d",
            report.total(),
            report.cancelled_orphan_jobs,
            report.cancelled_delete_jobs,
            report.resurrected_files,
            report.completed_deletes,
            report.snapped_counters,
            report.retried_errors,
        )
    else:
        logger.info("startup recovery: state consistent, nothing to repair")
    return report
