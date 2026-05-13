"""Read and parse ``<filename>.voitta.meta`` sidecar files.

A sidecar is a JSON object placed next to an indexed file that carries
owner/provenance metadata the indexer can't derive from the file itself.
When present, the metadata is:

* Merged into every Qdrant chunk payload as ``meta_*`` prefixed fields so
  clients can filter/facet by owner, tags, source system, etc.
* Used to override the file's ``mtime_ns`` (from ``modified``) and
  ``created_at_ns`` (from ``created``) so temporal queries reflect the
  document's own timeline rather than filesystem ingest time.

All fields are optional — partial sidecars are valid.  Malformed JSON is
logged as a warning and the sidecar is silently ignored (file is indexed
normally without meta fields).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ISO 8601 fields whose values become timestamp overrides (nanoseconds).
_TEMPORAL_FIELDS = {"created", "modified"}

# Non-temporal fields copied verbatim into the Qdrant payload under
# a ``meta_`` prefix.  Keys not listed here are silently ignored so
# future sidecar fields don't pollute payloads unexpectedly.
_PAYLOAD_FIELDS = {
    "owner",
    "owner_email",
    "owner_role",
    "system",
    "version",
    "tags",
    "shared_with",
    "file",
}


def _parse_iso_ns(value: Any) -> int | None:
    """Parse an ISO 8601 string to nanoseconds since epoch, or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError):
        return None


@dataclass
class FileMeta:
    """Parsed contents of a ``.voitta.meta`` sidecar."""

    # Timestamp overrides (nanoseconds).  None means "use filesystem default".
    created_at_ns: int | None = None
    modified_at_ns: int | None = None

    # Flat dict of non-temporal fields ready to merge into Qdrant payload.
    # Keys are already ``meta_``-prefixed.
    payload_fields: dict[str, Any] = field(default_factory=dict)


def sidecar_path(file_path: Path) -> Path:
    """Return the sidecar path for *file_path*."""
    return file_path.parent / (file_path.name + ".voitta.meta")


def load(file_path: Path) -> FileMeta | None:
    """Load and parse the sidecar for *file_path*.

    Returns ``None`` if no sidecar exists.  Logs a warning and returns
    ``None`` if the sidecar exists but is invalid JSON or not an object.
    """
    p = sidecar_path(file_path)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("meta_sidecar: failed to parse %s: %s", p, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("meta_sidecar: %s is not a JSON object — ignored", p)
        return None

    meta = FileMeta()
    meta.created_at_ns = _parse_iso_ns(data.get("created"))
    meta.modified_at_ns = _parse_iso_ns(data.get("modified"))

    for key in _PAYLOAD_FIELDS:
        if key in data:
            meta.payload_fields[f"meta_{key}"] = data[key]

    logger.debug(
        "meta_sidecar loaded: %s  created=%s modified=%s fields=%s",
        p.name,
        data.get("created"),
        data.get("modified"),
        list(meta.payload_fields),
    )
    return meta
