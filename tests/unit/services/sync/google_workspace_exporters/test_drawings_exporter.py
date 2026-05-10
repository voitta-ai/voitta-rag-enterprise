"""DrawingExporter — Drive Drawings → PNG export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voitta_rag_enterprise.services.sync.google_workspace_exporters import (
    ExportContext,
    ProducerContext,
    get_default_registry,
)
from voitta_rag_enterprise.services.sync.google_workspace_exporters.drawings import (
    DrawingExporter,
)


def _ctx(tmp_path: Path) -> ExportContext:
    return ExportContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: None,
        forms=lambda: None,
        drive_thread_local=lambda: None,
        access_token="fake",
    )


def _producer_ctx(tmp_path: Path) -> ProducerContext:
    return ProducerContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: None,
        slides=lambda: None,
        forms=lambda: None,
        access_token="fake",
    )


def _drive_item(item_id: str, name: str) -> dict:
    return {
        "id": item_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.drawing",
        "modifiedTime": "2026-05-10T00:00:00Z",
        "webViewLink": f"https://docs.google.com/drawings/d/{item_id}/edit",
    }


def test_registry_dispatches_drawing_mime() -> None:
    r = get_default_registry()
    found = r.find("application/vnd.google-apps.drawing")
    assert isinstance(found, DrawingExporter)


def test_export_returns_one_png_entry(tmp_path: Path) -> None:
    entries = DrawingExporter().export(_drive_item("d1", "Diagram"), "Diagram", _ctx(tmp_path))
    assert len(entries) == 1
    e = entries[0]
    assert e.rel_path == "Diagram.png"
    assert e.fingerprint == "2026-05-10T00:00:00Z"
    assert "drawings/d/d1" in e.url


def test_png_producer_streams_via_export_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OneShotDownloader:
        def __init__(self, fh, request: Any) -> None:
            self._fh = fh
            self._done = False

        def next_chunk(self):
            if self._done:
                return None, True
            self._fh.write(b"PNG-BYTES")
            self._done = True
            return None, True

    import googleapiclient.http

    monkeypatch.setattr(googleapiclient.http, "MediaIoBaseDownload", _OneShotDownloader)

    captured: dict[str, Any] = {}

    class _FakeFiles:
        def export_media(self, *, fileId: str, mimeType: str) -> object:
            captured["fileId"] = fileId
            captured["mimeType"] = mimeType
            return object()

    class _FakeDrive:
        def files(self):
            return _FakeFiles()

    entry = DrawingExporter().export(_drive_item("d1", "Diagram"), "Diagram", _ctx(tmp_path))[0]
    dest = tmp_path / entry.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    entry.producer(dest, drive=_FakeDrive(), ctx=_producer_ctx(tmp_path))
    assert dest.read_bytes() == b"PNG-BYTES"
    assert captured == {"fileId": "d1", "mimeType": "image/png"}
