"""Unit tests for the Google Drive connector.

These tests stub the Drive + Docs services entirely — the goal is to verify
the *connector logic* (native-type fan-out, tab → file mapping, sidecar
shape, mirror semantics) without going to the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voitta_image_rag.services.sync.google_drive import (
    NATIVE_DOC,
    NATIVE_FOLDER,
    NATIVE_SHEET,
    NATIVE_SLIDES,
    GoogleDriveAuth,
    GoogleDriveConnector,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFiles:
    def __init__(self, listings: dict[str, list[dict[str, Any]]],
                 export_payloads: dict[str, bytes],
                 download_payloads: dict[str, bytes]) -> None:
        self._listings = listings
        self._export_payloads = export_payloads
        self._download_payloads = download_payloads

    def list(self, **kwargs):
        q = kwargs["q"]
        # q looks like: "'<folder_id>' in parents and trashed=false"
        folder_id = q.split("'")[1]
        files = self._listings.get(folder_id, [])
        return _FakeRequest({"files": files})

    def export_media(self, *, fileId: str, mimeType: str):
        return _FakeMediaRequest(self._export_payloads[fileId])

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True):
        return _FakeMediaRequest(self._download_payloads[fileId])


class _FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class _FakeMediaRequest:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._consumed = False


class _FakeDownloader:
    """Stand-in for ``MediaIoBaseDownload``. One-shot — writes the request body."""

    def __init__(self, fh, request: _FakeMediaRequest) -> None:
        self._fh = fh
        self._request = request

    def next_chunk(self):
        if self._request._consumed:
            return None, True
        self._fh.write(self._request._body)
        self._request._consumed = True
        return None, True


class _FakeDocs:
    def __init__(self, doc_responses: dict[str, dict[str, Any]]) -> None:
        self._docs = doc_responses

    def documents(self):
        return self

    def get(self, *, documentId: str, includeTabsContent: bool):
        return _FakeRequest(self._docs[documentId])


class _FakeDrive:
    def __init__(self, files: _FakeFiles) -> None:
        self._files = files

    def files(self):
        return self._files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_media_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``MediaIoBaseDownload`` with our fake one in both producers."""
    import googleapiclient.http

    monkeypatch.setattr(googleapiclient.http, "MediaIoBaseDownload", _FakeDownloader)


def _make_auth() -> GoogleDriveAuth:
    return GoogleDriveAuth(
        client_id="cid",
        client_secret="secret",
        refresh_token="rt",
    )


def _patch_services(
    connector: GoogleDriveConnector,
    drive: _FakeDrive,
    docs: _FakeDocs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connector, "_build_services", lambda *a, **k: (drive, docs)
    )
    monkeypatch.setattr(
        connector, "_sync_access_token", lambda *a, **k: "fake-token"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_types_export_to_expected_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "sheet1",
                "name": "Q4",
                "mimeType": NATIVE_SHEET,
                "modifiedTime": "2026-01-01T00:00:00Z",
                "webViewLink": "https://docs.google.com/spreadsheets/d/sheet1/edit",
            },
            {
                "id": "slides1",
                "name": "Pitch",
                "mimeType": NATIVE_SLIDES,
                "modifiedTime": "2026-01-01T00:00:00Z",
                "webViewLink": "https://docs.google.com/presentation/d/slides1/edit",
            },
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={"sheet1": b"xlsx-bytes", "slides1": b"pptx-bytes"},
            download_payloads={},
        )
    )
    docs = _FakeDocs({})

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, docs, monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    root = (tmp_path / "root").resolve()
    assert (root / "Root" / "Q4.xlsx").read_bytes() == b"xlsx-bytes"
    assert (root / "Root" / "Pitch.pptx").read_bytes() == b"pptx-bytes"
    sidecar = json.loads((root / ".voitta_sources.json").read_text())
    assert sidecar["Root/Q4.xlsx"]["url"] == "https://docs.google.com/spreadsheets/d/sheet1/edit"
    # Non-tab files don't carry a `tab` key.
    assert "tab" not in sidecar["Root/Q4.xlsx"]


@pytest.mark.asyncio
async def test_multi_tab_doc_writes_one_md_per_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "doc1",
                "name": "Specs",
                "mimeType": NATIVE_DOC,
                "modifiedTime": "2026-01-02T00:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc1/edit",
            }
        ]
    }
    docs_payload = {
        "doc1": {
            "tabs": [
                {
                    "tabProperties": {"tabId": "t.intro", "title": "Intro"},
                    "documentTab": {
                        "body": {
                            "content": [
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "hello", "textStyle": {}}}
                                        ]
                                    }
                                }
                            ]
                        }
                    },
                },
                {
                    "tabProperties": {"tabId": "t.api", "title": "API"},
                    "documentTab": {
                        "body": {
                            "content": [
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "endpoints", "textStyle": {}}}
                                        ]
                                    }
                                }
                            ]
                        }
                    },
                },
            ]
        }
    }
    drive = _FakeDrive(
        _FakeFiles(listings, export_payloads={}, download_payloads={})
    )
    docs = _FakeDocs(docs_payload)
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, docs, monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    assert stats.tabs_written == 2

    root = (tmp_path / "root").resolve()
    intro = root / "Root" / "Specs" / "01-Intro.md"
    api = root / "Root" / "Specs" / "02-API.md"
    assert intro.exists() and api.exists()
    assert "hello" in intro.read_text()
    assert "endpoints" in api.read_text()
    # Fingerprint header lines must be present so the next sync skips work.
    assert intro.read_text().splitlines()[0].startswith("<!--voitta-fingerprint:")

    sidecar = json.loads((root / ".voitta_sources.json").read_text())
    assert sidecar["Root/Specs/01-Intro.md"]["tab"] == "Intro"
    assert sidecar["Root/Specs/02-API.md"]["tab"] == "API"
    # URL deep-links into the right tab.
    assert "tab=t.intro" in sidecar["Root/Specs/01-Intro.md"]["url"]
    assert "tab=t.api" in sidecar["Root/Specs/02-API.md"]["url"]


@pytest.mark.asyncio
async def test_doc_without_tabs_falls_back_to_docx_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "doc2",
                "name": "Plain",
                "mimeType": NATIVE_DOC,
                "modifiedTime": "2026-01-03T00:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc2/edit",
            }
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={"doc2": b"docx-bytes"},
            download_payloads={},
        )
    )
    # Empty `tabs` array → fallback to docx export.
    docs = _FakeDocs({"doc2": {"tabs": []}})
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, docs, monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    assert stats.tabs_written == 0
    root = (tmp_path / "root").resolve()
    assert (root / "Root" / "Plain.docx").read_bytes() == b"docx-bytes"


@pytest.mark.asyncio
async def test_subfolder_recursion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "sub",
                "name": "child",
                "mimeType": NATIVE_FOLDER,
                "modifiedTime": "2026-01-01T00:00:00Z",
            }
        ],
        "sub": [
            {
                "id": "f1",
                "name": "note.txt",
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "abc",
                "webViewLink": "https://drive.google.com/file/d/f1/view",
            }
        ],
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"f1": b"hello"},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    root = (tmp_path / "root").resolve()
    assert (root / "Root" / "child" / "note.txt").read_bytes() == b"hello"
    sidecar = json.loads((root / ".voitta_sources.json").read_text())
    assert sidecar["Root/child/note.txt"]["url"] == "https://drive.google.com/file/d/f1/view"


@pytest.mark.asyncio
async def test_unsupported_native_type_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "form1",
                "name": "Survey",
                "mimeType": "application/vnd.google-apps.form",
                "modifiedTime": "2026-01-01T00:00:00Z",
            },
            {
                "id": "f1",
                "name": "kept.bin",
                "mimeType": "application/octet-stream",
                "size": "3",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "x",
                "webViewLink": "https://drive.google.com/file/d/f1/view",
            },
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"f1": b"abc"},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    root = (tmp_path / "root").resolve()
    assert (root / "Root" / "kept.bin").exists()
    # The form should not have produced any local file.
    assert not list(root.rglob("Survey*"))


@pytest.mark.asyncio
async def test_mirror_deletes_locals_not_on_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "f1",
                "name": "kept.bin",
                "mimeType": "application/octet-stream",
                "size": "3",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "x",
            }
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"f1": b"abc"},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    root = (tmp_path / "root").resolve()
    root.mkdir(parents=True)
    stale = root / "stale.txt"
    stale.write_text("old")

    stats = await connector.sync(
        folder_root=root,
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.files_removed == 1
    assert not stale.exists()
    assert (root / "Root" / "kept.bin").exists()


@pytest.mark.asyncio
async def test_multiple_folders_each_get_own_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two picked folders → two top-level subdirectories, no collisions."""
    listings = {
        "A": [
            {
                "id": "a1",
                "name": "shared.bin",
                "mimeType": "application/octet-stream",
                "size": "3",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "x",
            },
        ],
        "B": [
            # Same filename in a different Drive folder — must NOT collide.
            {
                "id": "b1",
                "name": "shared.bin",
                "mimeType": "application/octet-stream",
                "size": "3",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "y",
            },
        ],
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"a1": b"AAA", "b1": b"BBB"},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[
            {"id": "A", "name": "Project Alpha"},
            {"id": "B", "name": "Project Beta"},
        ],
    )
    assert stats.errors == []
    root = (tmp_path / "root").resolve()
    assert (root / "Project Alpha" / "shared.bin").read_bytes() == b"AAA"
    assert (root / "Project Beta" / "shared.bin").read_bytes() == b"BBB"


def test_coerce_folders_field_handles_legacy_and_new_shapes() -> None:
    from voitta_image_rag.services.sync.google_drive import (
        coerce_folders_field,
        encode_folders_field,
    )

    # Empty / None → empty list
    assert coerce_folders_field(None) == []
    assert coerce_folders_field("") == []

    # Legacy plain-string folder ID → wrapped
    assert coerce_folders_field("0AB123") == [{"id": "0AB123", "name": ""}]

    # New JSON-array shape round-trips
    encoded = encode_folders_field(
        [{"id": "x", "name": "X"}, {"id": "y", "name": "Y"}]
    )
    assert coerce_folders_field(encoded) == [
        {"id": "x", "name": "X"},
        {"id": "y", "name": "Y"},
    ]

    # Empty / drop-id entries are filtered on encode
    assert encode_folders_field([{"id": "", "name": ""}]) is None
    assert encode_folders_field(None) is None
