"""Google Drawings (``vnd.google-apps.drawing``) → PNG export.

Drawings have no usable text representation; we export the visual to PNG
and let :class:`~voitta_rag_enterprise.services.parsers.image_parser.ImageFileParser`
handle indexing, exactly like a user-uploaded PNG.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import (
    ExportContext,
    NativeDriveExporter,
    ProducerContext,
    RemoteEntry,
)


PNG_MIME = "image/png"


class DrawingExporter(NativeDriveExporter):
    """Export a Google Drawing as a single PNG file."""

    mime_type = "application/vnd.google-apps.drawing"

    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,
        ctx: ExportContext,  # noqa: ARG002
    ) -> list[RemoteEntry]:
        drawing_id = item["id"]
        modified_time = item.get("modifiedTime", "")
        web_url = (
            item.get("webViewLink")
            or f"https://docs.google.com/drawings/d/{drawing_id}/edit"
        )
        return [
            RemoteEntry(
                rel_path=f"{rel_no_ext}.png",
                url=web_url,
                fingerprint=modified_time,
                tab=None,
                producer=_make_png_producer(drawing_id),
            )
        ]


def _make_png_producer(
    drawing_id: str,
) -> Callable[[Path, Any, ProducerContext], None]:
    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        from googleapiclient.http import MediaIoBaseDownload

        tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
        try:
            with tmp.open("wb") as f:
                request = drive.files().export_media(
                    fileId=drawing_id, mimeType=PNG_MIME
                )
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            os.replace(tmp, dest)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    return _produce
