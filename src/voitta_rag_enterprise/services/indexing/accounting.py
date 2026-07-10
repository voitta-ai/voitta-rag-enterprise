"""Pending-embed accounting: the round-token contract.

Embed "round tokens" keep a file's ``pending_embeds`` counter honest across
re-extracts. The contract has three parts, split across modules:

- **MINTED** inside :func:`..extract._commit_indexing`'s transaction, which
  bumps ``embed_round`` and resets ``pending_embeds`` together. That stays in
  ``extract`` because moving it would change the transaction structure.
- **GUARDED** here, via the atomic round-checked ``UPDATE`` in
  :func:`_decrement_pending_embeds`: a decrement carrying a stale round matches
  no row and is ignored, so a late embed from a superseded extract can't corrupt
  the new counter.
- **REPAIRED** by :func:`..recovery.reconcile_abandoned_extracts`, which resets
  files stuck mid-pipeline when their extract job died.
"""

from __future__ import annotations

from sqlalchemy import text

from ...db.database import session_scope
from .common import logger, publish_file_upserted


def _decrement_pending_embeds(file_id: int, round_token: int | None = None) -> None:
    """Decrement ``pending_embeds`` from a finishing embed job.

    The decrement is performed via an atomic SQL ``UPDATE`` so concurrent
    callers cannot lose updates — a read-modify-write through the ORM is racy
    when many embed jobs finish in the same SQLite WAL window (which happens
    constantly: image embeds that hit the dedup path complete in ~1 ms each).

    The same UPDATE doubles as the round-token guard: when a re-extract has
    already bumped ``embed_round``, ``rowcount`` comes back as 0 and we skip
    the state-transition check entirely.

    On reaching zero, transitions the file to ``indexed`` and clears any
    previously-recorded ``error`` (a successful embed cycle supersedes a
    transient failure that has since been retried).
    """
    params: dict[str, object] = {"id": file_id}
    guard = ""
    if round_token is not None:
        guard = " AND embed_round = :r"
        params["r"] = round_token
    state_changed = False
    with session_scope() as s:
        res = s.execute(
            text(
                "UPDATE files SET pending_embeds = MAX(0, pending_embeds - 1) "
                "WHERE id = :id" + guard
            ),
            params,
        )
        if res.rowcount == 0:
            logger.debug(
                "skip pending decrement: file gone or stale round (job=%s)",
                round_token,
            )
            return
        row = s.execute(
            text("SELECT pending_embeds, state FROM files WHERE id = :id"),
            {"id": file_id},
        ).first()
        if row is None:
            return
        pending, state = row
        if pending == 0 and state in ("extracted", "embedding", "error"):
            s.execute(
                text(
                    "UPDATE files SET state = 'indexed', error = NULL "
                    "WHERE id = :id"
                ),
                {"id": file_id},
            )
            state_changed = True
    # Only emit when the file's user-visible state actually changed.
    # Mid-run pending_embeds decrements (e.g. 17 -> 16) are noise the UI
    # neither displays nor needs — and at 24 workers each PDF can produce
    # 50+ such decrements per file. Coalescing in events.py would already
    # squash them, but skipping the publish entirely is cheaper.
    if state_changed:
        publish_file_upserted(file_id)
