"""The job-kind → handler table is the worker's dispatch contract.

``main.py`` merges ``{**DEFAULT_HANDLERS, **INDEXING_HANDLERS}`` and hands the
result to the worker pool; a job kind that silently drops out of the merged
dict would leave enqueued jobs claimed-then-ignored. Freeze both halves.
"""

from __future__ import annotations

import inspect


def test_indexing_handlers_complete_and_async() -> None:
    from voitta_rag_enterprise.services.indexing import HANDLERS

    assert set(HANDLERS) == {
        "extract",
        "embed_text",
        "embed_image",
        "delete_file",
        "sync",
        "reindex_folder",
    }
    for kind, fn in HANDLERS.items():
        assert inspect.iscoroutinefunction(fn), f"{kind} handler must be async"


def test_merged_handlers_cover_every_default_kind() -> None:
    """Mirrors main.py's merge — every kind the worker may claim has a handler."""
    from voitta_rag_enterprise.services.indexing import HANDLERS
    from voitta_rag_enterprise.services.worker import DEFAULT_HANDLERS

    merged = {**DEFAULT_HANDLERS, **HANDLERS}
    assert "gc_cas" in merged
    assert set(DEFAULT_HANDLERS) <= set(merged)
    for kind, fn in merged.items():
        assert inspect.iscoroutinefunction(fn), f"{kind} handler must be async"
