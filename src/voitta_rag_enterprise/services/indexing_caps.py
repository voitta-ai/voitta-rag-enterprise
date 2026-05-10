"""Admin-managed indexing caps persisted as a single JSON file.

One file: ``<data_dir>/admin/indexing_caps.json``. Same atomic-write pattern
as :mod:`admin_store` (write to ``.tmp`` next to it, then ``os.replace``).

Why a separate store and not :class:`Settings`?
-----------------------------------------------
:class:`Settings` is env-driven and resolved once at boot. Admin-tunable
knobs need runtime mutation (no restart), which env vars don't give us.
This module is the runtime source of truth for those knobs; defaults are
sourced from :class:`Settings` where one already exists (so an existing
``VOITTA_MAX_FILE_BYTES`` env var keeps working until an admin overrides
it through the UI).

Caching: the JSON is read once and cached in process. :func:`update` clears
the cache after writing, so the next :func:`get_caps` call sees the new
values. There is no cross-process invalidation — admin-edited caps take
effect immediately in the editing process and on the next start everywhere
else. In single-process deploys (the only shape we ship today) that's
fine; the moment we go multi-replica, swap this for a pubsub.

Schema versioning: keys with unknown names are dropped on read. New keys
go in with their default on first :func:`get_caps`. No migration script
needed — the file is just an override set.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..config import get_settings

logger = logging.getLogger(__name__)

CAPS_FILENAME = "indexing_caps.json"

# Extensions that produce huge chunk counts when treated as text. A 142 MB
# JSON yielded 81 719 chunks in the wild and dominated the index without
# being useful as RAG content. Any file with one of these extensions whose
# size exceeds :data:`IndexingCaps.data_file_max_bytes` is parked in
# ``state='unsupported'`` at extract time rather than being chunked.
DATA_EXTENSIONS: frozenset[str] = frozenset({
    ".json",
    ".jsonl",
    ".ndjson",
    ".csv",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
})


@dataclass(frozen=True)
class IndexingCaps:
    """Snapshot of every admin-tunable indexing knob.

    Field-level defaults are the values shipped in code today; the override
    file only carries deltas. The dataclass is frozen so callers can't
    mutate the cached snapshot.
    """

    # Per-file size cap (global, scanner-enforced). Default 1 GiB.
    max_file_bytes: int = 1024 * 1024 * 1024

    # Data-file size cap (json/csv/tsv/xml/yaml). Default 5 MiB — well
    # above any config file and well below the JSON that triggered the
    # 81k-chunk pathology. Set to 0 to disable the special case.
    data_file_max_bytes: int = 5 * 1024 * 1024

    # XLSX per-sheet limits.
    xlsx_max_rows: int = 50_000
    xlsx_max_cols: int = 64

    # IPYNB per-cell output cap.
    ipynb_max_output_chars: int = 2_000

    # PDF rendering / parsing.
    pdf_pages_per_bucket: int = 20
    pdf_parse_timeout_s: int = 600
    pdf_page_render_long_edge_px: int = 1024
    pdf_page_render_webp_quality: int = 75


# Bounds for client-side validation; the PATCH route applies the same
# checks server-side. Keeping the table here lets the UI hint at sane
# ranges next to each input.
BOUNDS: dict[str, tuple[int, int]] = {
    # 1 KiB .. 16 GiB
    "max_file_bytes": (1024, 16 * 1024 * 1024 * 1024),
    # 0 (disabled) .. 1 GiB. 0 means "do not cap data files specially".
    "data_file_max_bytes": (0, 1024 * 1024 * 1024),
    "xlsx_max_rows": (100, 1_000_000),
    "xlsx_max_cols": (4, 1024),
    "ipynb_max_output_chars": (0, 100_000),
    "pdf_pages_per_bucket": (1, 200),
    "pdf_parse_timeout_s": (10, 7_200),
    "pdf_page_render_long_edge_px": (256, 4_096),
    "pdf_page_render_webp_quality": (10, 100),
}


_lock = threading.Lock()
_cache: IndexingCaps | None = None


def _path() -> Path:
    p = get_settings().data_dir / "admin" / CAPS_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _env_defaults() -> dict[str, int]:
    """Defaults that mirror :class:`Settings`, so an admin who hasn't yet
    touched the caps file sees the same values the existing env vars
    produce. Only fields that have a matching Settings attribute are
    sourced here; the rest fall back to the dataclass defaults.
    """
    s = get_settings()
    overlay: dict[str, int] = {}
    for fname in ("max_file_bytes", "pdf_pages_per_bucket", "pdf_parse_timeout_s",
                  "pdf_page_render_long_edge_px", "pdf_page_render_webp_quality"):
        if hasattr(s, fname):
            overlay[fname] = getattr(s, fname)
    return overlay


def _load_from_disk() -> dict[str, int]:
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text() or "{}")
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("indexing_caps parse failed at %s: %s", p, e)
        return {}
    if not isinstance(raw, dict):
        return {}
    known = {f.name for f in fields(IndexingCaps)}
    out: dict[str, int] = {}
    for k, v in raw.items():
        if k not in known:
            continue
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _coerce_and_clamp(name: str, value: int) -> int:
    if name in BOUNDS:
        lo, hi = BOUNDS[name]
        if value < lo:
            value = lo
        elif value > hi:
            value = hi
    return value


def get_caps() -> IndexingCaps:
    """Return the current cap snapshot. Cached after the first read."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        overlay = {**_env_defaults(), **_load_from_disk()}
        clamped = {k: _coerce_and_clamp(k, v) for k, v in overlay.items()}
        _cache = IndexingCaps(**{k: v for k, v in clamped.items()
                                 if k in {f.name for f in fields(IndexingCaps)}})
        return _cache


def invalidate_cache() -> None:
    """Drop the in-process cache so the next :func:`get_caps` re-reads.

    Exposed for tests; production callers go through :func:`update`.
    """
    global _cache
    with _lock:
        _cache = None


def update(partial: dict[str, int]) -> IndexingCaps:
    """Merge ``partial`` over the override file, clamp, persist, return new caps.

    Unknown keys are dropped silently. Non-integer values raise
    :class:`ValueError` — the route handler turns that into a 400.
    """
    known = {f.name for f in fields(IndexingCaps)}
    cleaned: dict[str, int] = {}
    for k, v in partial.items():
        if k not in known:
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{k}: expected integer, got {type(v).__name__}")
        cleaned[k] = _coerce_and_clamp(k, v)

    existing = _load_from_disk()
    merged = {**existing, **cleaned}

    p = _path()
    body = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise

    invalidate_cache()
    return get_caps()


def as_dict() -> dict[str, int]:
    """Convenience: caps snapshot as a plain dict (for JSON responses)."""
    return asdict(get_caps())


def defaults_dict() -> dict[str, int]:
    """The shipped defaults — used by the UI to render a "reset to default"
    affordance next to each row."""
    return asdict(IndexingCaps())


def bounds_dict() -> dict[str, list[int]]:
    """Bounds table as JSON-friendly lists, for the UI's input ``min``/``max``."""
    return {k: [lo, hi] for k, (lo, hi) in BOUNDS.items()}
