"""Source-object provenance: capture at sync time, surface in the Qdrant payload.

Synced objects (Google Drive, SharePoint, …) carry provenance the local file
doesn't: who owns/created it, who last edited it, who shared the synced root,
and the source's created/modified timestamps. Connectors capture these into a
small normalized dict stored as JSON on ``File.source_meta``; at index time
``payload_fields`` flattens it into ``meta_*`` Qdrant payload keys so search can
prefilter/sort by owner or date.

Two helpers keep connectors and the indexer in agreement on the shape:
- :func:`build` — connectors call this to normalize raw API values into the
  stored dict (ISO timestamps → epoch seconds; blanks dropped).
- :func:`payload_fields` — the indexer calls this to expand the stored dict into
  flat, null-omitted ``meta_*`` payload fields, adding ``meta_uploaded_ts``.

Field set (all optional; absent ⇒ omitted, never null in the payload):

  people (strings):  owner_name owner_email  editor_name editor_email
                     shared_by_name shared_by_email
  dates  (epoch s):  created_ts  modified_ts
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# The people keys carried in source_meta (stored without the ``meta_`` prefix;
# the prefix is added in payload_fields). Order is irrelevant.
_PEOPLE_KEYS = (
    "owner_name",
    "owner_email",
    "editor_name",
    "editor_email",
    "shared_by_name",
    "shared_by_email",
)
_DATE_KEYS = ("created_ts", "modified_ts")


def iso_to_epoch(value: Any) -> int | None:
    """ISO-8601 (RFC3339) string → integer epoch **seconds**, or None.

    Handles the trailing-``Z`` form Drive/Graph emit. Naive datetimes are
    assumed UTC. Returns None for anything unparseable so a bad source value
    never breaks a sync.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def build(
    *,
    owner_name: str | None = None,
    owner_email: str | None = None,
    editor_name: str | None = None,
    editor_email: str | None = None,
    shared_by_name: str | None = None,
    shared_by_email: str | None = None,
    created: Any = None,   # ISO string or epoch int
    modified: Any = None,  # ISO string or epoch int
) -> dict[str, Any]:
    """Normalize raw connector values into the stored ``source_meta`` dict.

    Blank/None people fields and unparseable dates are dropped, so the stored
    JSON only carries what's actually known. ``created``/``modified`` accept
    either an ISO string (converted to epoch seconds) or an int passed through.
    Returns ``{}`` when nothing is known (caller may store None instead).
    """
    out: dict[str, Any] = {}
    for key, val in (
        ("owner_name", owner_name),
        ("owner_email", owner_email),
        ("editor_name", editor_name),
        ("editor_email", editor_email),
        ("shared_by_name", shared_by_name),
        ("shared_by_email", shared_by_email),
    ):
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    for key, raw in (("created_ts", created), ("modified_ts", modified)):
        ts = raw if isinstance(raw, int) else iso_to_epoch(raw)
        if isinstance(ts, int):
            out[key] = ts
    return out


def payload_fields(
    source_meta: dict[str, Any] | None,
    *,
    uploaded_ts: int | None = None,
    modified_fallback_ts: int | None = None,
) -> dict[str, Any]:
    """Flatten stored ``source_meta`` into ``meta_*`` Qdrant payload fields.

    - People keys → ``meta_<key>`` verbatim (string).
    - ``created_ts``/``modified_ts`` → ``meta_created_ts``/``meta_modified_ts``.
    - ``meta_uploaded_ts`` from ``uploaded_ts`` (the file's ``added_at``).
    - ``modified_fallback_ts`` (filesystem mtime) fills ``meta_modified_ts``
      only when the source didn't provide one — so non-synced files still get a
      modified date.

    Null/absent values are omitted entirely (matching the ``layout_*``
    "missing, not null" convention) so the payload stays compact and exact
    filters don't match empty rows.
    """
    sm = source_meta or {}
    out: dict[str, Any] = {}
    for key in _PEOPLE_KEYS:
        v = sm.get(key)
        if isinstance(v, str) and v:
            out[f"meta_{key}"] = v
    if isinstance(sm.get("created_ts"), int):
        out["meta_created_ts"] = sm["created_ts"]
    if isinstance(sm.get("modified_ts"), int):
        out["meta_modified_ts"] = sm["modified_ts"]
    elif isinstance(modified_fallback_ts, int):
        out["meta_modified_ts"] = modified_fallback_ts
    if isinstance(uploaded_ts, int):
        out["meta_uploaded_ts"] = uploaded_ts
    return out


# Field names exposed for the vector-store payload-index declarations, so the
# index list and the producer can't drift apart.
PEOPLE_PAYLOAD_FIELDS = tuple(f"meta_{k}" for k in _PEOPLE_KEYS)
DATE_PAYLOAD_FIELDS = ("meta_created_ts", "meta_modified_ts", "meta_uploaded_ts")
