"""CAS-backed layout loaders: char→page anchors and per-page layout summaries."""

from __future__ import annotations

import json

from ...cas import store as cas_store
from .common import logger


def _load_char_to_page(file_cas_id: str | None) -> list[tuple[int, int]]:
    """Pull the parser's char→page anchors back from CAS (or ``[]``)."""
    if not file_cas_id:
        return []
    try:
        raw = cas_store.read_file_blob(file_cas_id, "char_to_page.json")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("char_to_page.json unparseable for cas=%s", file_cas_id)
        return []
    out: list[tuple[int, int]] = []
    for entry in data if isinstance(data, list) else []:
        if (
            isinstance(entry, list | tuple)
            and len(entry) == 2
            and isinstance(entry[0], int)
            and isinstance(entry[1], int)
        ):
            out.append((entry[0], entry[1]))
    return out


def _load_layout_summaries(file_cas_id: str | None) -> dict[int, dict]:
    """Pull per-page layout summaries back from CAS (or ``{}``).

    Stored as ``{"<page_int_str>": {layout_*: ...}}``; we re-parse the
    keys to ``int``. Pages without an entry just won't get layout
    fields attached to their chunks/images, which is the desired
    behaviour (consumer treats missing as "unknown layout").
    """
    if not file_cas_id:
        return {}
    try:
        raw = cas_store.read_file_blob(file_cas_id, "layout_summaries.json")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("layout_summaries.json unparseable for cas=%s", file_cas_id)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, dict] = {}
    for k, v in data.items():
        try:
            page = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            out[page] = v
    return out
