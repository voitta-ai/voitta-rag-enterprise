"""End-to-end ``extract`` job handler.

Reindex is whole-file: any change resets ``state='pending'`` and re-runs
the full extract → chunk → embed pipeline against the new bytes. Image
↔ chunk linkage is built here too: every extracted image carries an
anchor chunk (the chunk straddling its position in the markdown);
chunks within ``chunk_image_link_radius`` get a ``nearby_image`` link
with chunk-index distance as the score.
"""

from __future__ import annotations

from .accounting import _decrement_pending_embeds
from .common import (
    _publish_job_progress,
    _stage,
    file_event_payload,
    publish_file_upserted,
)
from .delete import run_delete_file, wipe_file_data
from .embed import run_embed_image, run_embed_text
from .extract import run_extract
from .layout import _load_char_to_page, _load_layout_summaries
from .recovery import reconcile_abandoned_extracts
from .reindex import run_reindex_folder
from .sync_job import run_sync

HANDLERS = {
    "extract": run_extract,
    "embed_text": run_embed_text,
    "embed_image": run_embed_image,
    "delete_file": run_delete_file,
    "sync": run_sync,
    "reindex_folder": run_reindex_folder,
}

__all__ = [
    "HANDLERS",
    "_decrement_pending_embeds",
    "_load_char_to_page",
    "_load_layout_summaries",
    "_publish_job_progress",
    "_stage",
    "file_event_payload",
    "publish_file_upserted",
    "reconcile_abandoned_extracts",
    "run_delete_file",
    "run_embed_image",
    "run_embed_text",
    "run_extract",
    "run_reindex_folder",
    "run_sync",
    "wipe_file_data",
]
