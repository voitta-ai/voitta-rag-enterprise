"""Sweep zero-refcount CAS entries after a quiet period."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import CasRef
from .store import remove_blob

logger = logging.getLogger(__name__)

DEFAULT_QUIET_PERIOD_S = 60


@dataclass
class GcResult:
    swept: int


def sweep(session: Session, quiet_period_s: int = DEFAULT_QUIET_PERIOD_S) -> GcResult:
    """Delete blobs whose refcount has been zero for ``quiet_period_s`` or longer."""
    cutoff = int(time.time()) - quiet_period_s
    stale = (
        session.execute(
            select(CasRef).where(
                CasRef.refcount == 0,
                CasRef.last_decref_at.is_not(None),
                CasRef.last_decref_at <= cutoff,
            )
        )
        .scalars()
        .all()
    )
    swept = 0
    for ref in stale:
        if remove_blob(ref.kind, ref.cas_id):
            swept += 1
        session.delete(ref)
    if swept:
        logger.info("cas gc swept %d blob(s)", swept)
    return GcResult(swept=swept)
