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

Connectors with rich structured attributes (Jira issues, Confluence pages)
additionally carry:

- ``attrs`` — a *curated* dict of filterable/sortable attributes (status,
  assignee, labels, …). ``payload_fields`` flattens each into an ``attr_<key>``
  Qdrant payload field; the canonical indexed set lives in ``ATTR_FILTER_FIELDS``
  so search can prefilter on them. Connectors only put curated keys here.
- ``attrs_raw`` — the full, un-curated attribute bag (every Jira ``customfield_*``,
  etc.). Stored verbatim and surfaced as a single ``attr_raw_json`` string so
  nothing is lost on retrieval, but *not* individually indexed (bounded index
  growth, by design).
"""

from __future__ import annotations

import json
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

# Curated attribute keys promoted from ``source_meta["attrs"]`` to indexed
# ``attr_<key>`` Qdrant payload fields, with their Qdrant index type. Connectors
# (Jira, Confluence) populate whatever subset applies; absent ⇒ omitted. List
# values index as "keyword" (Qdrant indexes each element, so "labels contains X"
# works). Keeping the canonical set here means the producer (payload_fields) and
# the index declarations (vector_store) can't drift apart.
_ATTR_FILTER_FIELDS: tuple[tuple[str, str], ...] = (
    # Jira issue attributes
    ("status", "keyword"),
    ("priority", "keyword"),
    ("assignee", "keyword"),
    ("reporter", "keyword"),
    ("issuetype", "keyword"),
    ("project", "keyword"),
    ("resolution", "keyword"),
    ("parent", "keyword"),
    ("epic", "keyword"),
    ("sprint", "keyword"),
    ("labels", "keyword"),            # list
    ("components", "keyword"),        # list
    ("fix_versions", "keyword"),      # list
    ("affects_versions", "keyword"),  # list
    ("votes", "integer"),
    ("watches", "integer"),
    ("story_points", "float"),
    # Confluence page attributes (added now so the index is ready when the
    # Confluence connector lands; harmless until something writes them).
    ("space", "keyword"),
    ("space_name", "keyword"),
    ("ancestors", "keyword"),         # list
    ("version", "integer"),
    ("content_type", "keyword"),
)
# Just the curated keys, for fast membership tests.
_ATTR_FILTER_KEYS = frozenset(k for k, _ in _ATTR_FILTER_FIELDS)


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
    attrs: dict[str, Any] | None = None,
    attrs_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize raw connector values into the stored ``source_meta`` dict.

    Blank/None people fields and unparseable dates are dropped, so the stored
    JSON only carries what's actually known. ``created``/``modified`` accept
    either an ISO string (converted to epoch seconds) or an int passed through.
    ``attrs`` (curated, filterable) and ``attrs_raw`` (full bag) are stored as
    nested dicts after dropping empty values. Returns ``{}`` when nothing is
    known (caller may store None instead).
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
    curated = _clean_attrs(attrs)
    if curated:
        out["attrs"] = curated
    raw_bag = _clean_attrs(attrs_raw)
    if raw_bag:
        out["attrs_raw"] = raw_bag
    return out


def _clean_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Drop None / empty-string / empty-list values from an attribute dict.

    Keeps 0 and False (they're meaningful), strips whitespace on strings, and
    de-blanks list elements. Mirrors the "missing, not null" convention so a
    connector can pass a wide dict and let absent values fall away.
    """
    if not isinstance(attrs, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in attrs.items():
        if val is None:
            continue
        if isinstance(val, str):
            v = val.strip()
            if v:
                out[key] = v
        elif isinstance(val, (list, tuple)):
            items = [str(x).strip() for x in val if str(x).strip()]
            if items:
                out[key] = items
        else:
            out[key] = val
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
    # Curated attributes → attr_<key> (each independently filterable). We emit
    # every key the connector put in ``attrs`` (so connectors can stash extra
    # retrievable attrs); only the keys in ATTR_FILTER_FIELDS are *indexed*.
    attrs = sm.get("attrs")
    if isinstance(attrs, dict):
        for key, val in _clean_attrs(attrs).items():
            out[f"attr_{key}"] = val
    # Full raw bag → a single JSON string. Retrievable, not indexed.
    raw_bag = sm.get("attrs_raw")
    if isinstance(raw_bag, dict) and raw_bag:
        out["attr_raw_json"] = json.dumps(raw_bag, sort_keys=True, default=str)
    return out


# Field names exposed for the vector-store payload-index declarations, so the
# index list and the producer can't drift apart.
PEOPLE_PAYLOAD_FIELDS = tuple(f"meta_{k}" for k in _PEOPLE_KEYS)
DATE_PAYLOAD_FIELDS = ("meta_created_ts", "meta_modified_ts", "meta_uploaded_ts")
# Curated attribute payload fields + their Qdrant index types, for the
# vector-store payload-index declarations. ``attr_raw_json`` is intentionally
# absent — it's stored but not indexed.
ATTR_PAYLOAD_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (f"attr_{k}", t) for k, t in _ATTR_FILTER_FIELDS
)
