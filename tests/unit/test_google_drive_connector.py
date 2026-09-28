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

from voitta_rag_enterprise.services.sync.google_drive import (
    NATIVE_DOC,
    NATIVE_FOLDER,
    NATIVE_SHEET,
    NATIVE_SLIDES,
    GoogleDriveAuth,
    GoogleDriveConnector,
    WorkspaceApiStatus,
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

    def get(self, *, documentId: str, includeTabsContent: bool):  # noqa: ARG002
        return _FakeRequest(self._docs[documentId])


class _FakeSheets:
    """``spreadsheets.get`` + ``spreadsheets.values.get`` for the
    SpreadsheetExporter integration. Configured with ``meta`` keyed by
    spreadsheet id (returned by ``get``) and ``values`` keyed by
    ``(spreadsheetId, range)``."""

    def __init__(
        self,
        meta: dict[str, dict[str, Any]] | None = None,
        values: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self._meta = meta or {}
        self._values = values or {}

    def spreadsheets(self):
        outer = self

        class _Spreadsheets:
            def get(self, *, spreadsheetId: str, fields: str = "") -> _FakeRequest:  # noqa: ARG002
                return _FakeRequest(outer._meta.get(spreadsheetId, {}))

            def values(self):
                class _Values:
                    def get(
                        self,
                        *,
                        spreadsheetId: str,
                        range: str,  # noqa: A002
                        valueRenderOption: str = "",  # noqa: ARG002
                        dateTimeRenderOption: str = "",  # noqa: ARG002
                    ) -> _FakeRequest:
                        return _FakeRequest(
                            outer._values.get((spreadsheetId, range), {"values": []})
                        )

                return _Values()

        return _Spreadsheets()


class _FakeSlides:
    """``presentations.get`` + ``pages.getThumbnail`` for the
    PresentationExporter integration."""

    def __init__(
        self,
        decks: dict[str, dict[str, Any]] | None = None,
        thumbnails: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self._decks = decks or {}
        self._thumbnails = thumbnails or {}

    def presentations(self):
        outer = self

        class _Presentations:
            def get(self, *, presentationId: str) -> _FakeRequest:
                return _FakeRequest(outer._decks.get(presentationId, {}))

            def pages(self):
                class _Pages:
                    def getThumbnail(
                        self,
                        *,
                        presentationId: str,
                        pageObjectId: str,
                        thumbnailProperties_thumbnailSize: str = "",  # noqa: ARG002
                    ) -> _FakeRequest:
                        return _FakeRequest(
                            outer._thumbnails.get((presentationId, pageObjectId), {})
                        )

                return _Pages()

        return _Presentations()


class _FakeForms:
    """``forms.get`` for the FormExporter integration."""

    def __init__(self, forms: dict[str, dict[str, Any]] | None = None) -> None:
        self._forms = forms or {}

    def forms(self):
        return self

    def get(self, *, formId: str) -> _FakeRequest:
        return _FakeRequest(self._forms.get(formId, {}))


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
    *,
    sheets: _FakeSheets | None = None,
    slides: _FakeSlides | None = None,
    forms: _FakeForms | None = None,
    api_status: WorkspaceApiStatus | None = None,
) -> None:
    """Patch every per-thread service builder so production code paths
    reach the fakes instead of trying to mint real Google credentials.

    Tests that don't exercise a particular Workspace API can leave its
    arg as ``None`` — the corresponding builder still gets patched (to
    return an empty fake) so the lazy factory inside the connector
    doesn't blow up if some unrelated code path triggers it.

    ``api_status`` overrides the result of the API capability probe the
    connector runs at sync start; the default reports every API enabled
    so native exporters run. Pass a partial status to simulate a project
    with some Workspace APIs disabled (graceful-degradation tests).
    """
    sheets = sheets or _FakeSheets()
    slides = slides or _FakeSlides()
    forms = forms or _FakeForms()
    monkeypatch.setattr(
        connector, "_build_services", lambda *a, **k: (drive, docs)
    )
    # Per-thread builders for the materialise pool and the download
    # pool. googleapiclient's Resource isn't thread-safe in production,
    # but the fakes here are pure-Python dicts — sharing one object
    # across the pool is fine.
    monkeypatch.setattr(
        connector, "_build_docs_for_thread", lambda *a, **k: docs
    )
    monkeypatch.setattr(
        connector, "_build_drive_for_thread", lambda *a, **k: drive
    )
    monkeypatch.setattr(
        connector, "_build_sheets_for_thread", lambda *a, **k: sheets
    )
    monkeypatch.setattr(
        connector, "_build_slides_for_thread", lambda *a, **k: slides
    )
    monkeypatch.setattr(
        connector, "_build_forms_for_thread", lambda *a, **k: forms
    )
    monkeypatch.setattr(
        connector, "_sync_access_token", lambda *a, **k: "fake-token"
    )
    # The API probe / preflight run real ``execute()`` calls against the
    # production-side service stubs to detect which Workspace APIs are
    # enabled. Our fakes here don't model that, so we stub the probe to a
    # fixed status (all-enabled by default) and no-op the Drive-only
    # preflight. Dedicated probe/preflight coverage lives in
    # ``test_google_drive_preflight``.
    import voitta_rag_enterprise.services.sync.google_drive as _gd

    status = api_status or WorkspaceApiStatus(
        drive=True, docs=True, sheets=True, slides=True, forms=True
    )
    monkeypatch.setattr(_gd, "probe_workspace_apis", lambda **kwargs: status)
    monkeypatch.setattr(_gd, "preflight_workspace_apis", lambda **kwargs: None)
    monkeypatch.setattr(_gd, "preflight_drive_only", lambda drive: None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sheets_render_per_sheet_md_plus_full_workbook_xlsx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Sheet lands as one .md per sheet under ``<stem>/`` plus the
    full workbook xlsx under ``.voitta_workbooks/``."""
    listings = {
        "ROOT": [
            {
                "id": "sheet1",
                "name": "Q4",
                "mimeType": NATIVE_SHEET,
                "modifiedTime": "2026-05-10T00:00:00Z",
                "webViewLink": "https://docs.google.com/spreadsheets/d/sheet1/edit",
            },
        ]
    }
    sheets_meta = {
        "sheet1": {
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Sales",
                        "gridProperties": {"rowCount": 3, "columnCount": 2},
                    }
                },
                {
                    "properties": {
                        "sheetId": 7,
                        "title": "Marketing",
                        "gridProperties": {"rowCount": 2, "columnCount": 2},
                    }
                },
            ]
        }
    }
    sheets_values = {
        ("sheet1", "'Sales'!A1:ZZ100"): {"values": [["Region", "Q4"], ["EU", "1000"]]},
        ("sheet1", "'Marketing'!A1:ZZ100"): {"values": [["Channel"], ["Email"]]},
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={"sheet1": b"XLSX-BYTES"},
            download_payloads={},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(
        connector,
        drive,
        _FakeDocs({}),
        monkeypatch,
        sheets=_FakeSheets(meta=sheets_meta, values=sheets_values),
    )

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    root = (tmp_path / "root").resolve()
    sales_md = (root / "Root" / "Q4" / "01-Sales.md").read_text()
    marketing_md = (root / "Root" / "Q4" / "02-Marketing.md").read_text()
    assert "| Region | Q4 |" in sales_md
    assert "| EU | 1000 |" in sales_md
    assert "| Channel |" in marketing_md
    # Full workbook lands under the sidecar dir.
    xlsx_path = root / ".voitta_workbooks" / "Root" / "Q4.xlsx"
    assert xlsx_path.read_bytes() == b"XLSX-BYTES"

    sidecar = json.loads((root / ".voitta_sources.json").read_text())
    # Per-sheet sidecar entries deep-link by gid.
    assert "#gid=0" in sidecar["Root/Q4/01-Sales.md"]["url"]
    assert sidecar["Root/Q4/01-Sales.md"]["tab"] == "Sales"
    # Full xlsx is in the sidecar too — clients can discover it.
    assert ".voitta_workbooks/Root/Q4.xlsx" in sidecar


@pytest.mark.asyncio
async def test_slides_render_per_slide_md_plus_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Slides deck lands as one .md per slide plus per-slide PNG."""
    import httpx

    captured: dict[str, Any] = {}

    class _MockClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url: str) -> httpx.Response:
            captured["url"] = url
            return httpx.Response(200, content=b"PNG", request=httpx.Request("GET", url))

    monkeypatch.setattr(
        "voitta_rag_enterprise.services.sync.google_workspace_exporters.slides.httpx.Client",
        _MockClient,
    )

    listings = {
        "ROOT": [
            {
                "id": "deck1",
                "name": "Pitch",
                "mimeType": NATIVE_SLIDES,
                "modifiedTime": "2026-05-10T00:00:00Z",
                "webViewLink": "https://docs.google.com/presentation/d/deck1/edit",
            }
        ]
    }
    decks = {
        "deck1": {
            "slides": [
                {
                    "objectId": "p1",
                    "pageElements": [
                        {
                            "shape": {
                                "placeholder": {"type": "TITLE"},
                                "text": {"textElements": [{"textRun": {"content": "Intro"}}]},
                            }
                        },
                        {
                            "shape": {
                                "text": {"textElements": [{"textRun": {"content": "Welcome."}}]},
                            }
                        },
                    ],
                }
            ]
        }
    }
    thumbnails = {("deck1", "p1"): {"contentUrl": "https://lh3.googleusercontent.com/x"}}
    drive = _FakeDrive(_FakeFiles(listings, export_payloads={}, download_payloads={}))
    connector = GoogleDriveConnector()
    _patch_services(
        connector, drive, _FakeDocs({}), monkeypatch,
        slides=_FakeSlides(decks=decks, thumbnails=thumbnails),
    )

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    root = (tmp_path / "root").resolve()
    md = (root / "Root" / "Pitch" / "01-Intro.md").read_text()
    assert "# Slide 1: Intro" in md
    assert "Welcome." in md
    png = (root / "Root" / "Pitch" / "images" / "slide_1.png").read_bytes()
    assert png == b"PNG"
    sidecar = json.loads((root / ".voitta_sources.json").read_text())
    assert "#slide=id.p1" in sidecar["Root/Pitch/01-Intro.md"]["url"]


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
                "createdTime": "2026-01-01T00:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc1/edit",
                "owners": [{"displayName": "Roman", "emailAddress": "roman@x.com"}],
                "lastModifyingUser": {"displayName": "Roman", "emailAddress": "roman@x.com"},
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
    # Native exports now carry source provenance too (was a gap — only binary
    # files used to). Both tab files inherit the parent doc's owner + dates.
    for rel in ("Root/Specs/01-Intro.md", "Root/Specs/02-API.md"):
        assert sidecar[rel]["owner_email"] == "roman@x.com"
        assert sidecar[rel]["created_ts"] == 1767225600   # 2026-01-01Z
        assert sidecar[rel]["modified_ts"] == 1767312000   # 2026-01-02Z


@pytest.mark.asyncio
async def test_doc_without_tabs_renders_a_single_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doc returned by ``documents.get`` with no tabs structure (legacy
    docs created before the tabs feature) renders to a single
    ``<stem>.md`` directly. No docx fallback exists in the new path."""
    listings = {
        "ROOT": [
            {
                "id": "doc2",
                "name": "Plain",
                "mimeType": NATIVE_DOC,
                "modifiedTime": "2026-05-10T00:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc2/edit",
            }
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={},
        )
    )
    # No ``tabs`` key — Docs API surfaces ``body`` directly.
    docs = _FakeDocs({
        "doc2": {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "legacy body", "textStyle": {}}}]
                        }
                    }
                ]
            }
        }
    })
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, docs, monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    # No tabs → no per-tab markdown counted in tabs_written.
    assert stats.tabs_written == 0
    root = (tmp_path / "root").resolve()
    md = (root / "Root" / "Plain.md").read_text()
    assert "legacy body" in md


@pytest.mark.asyncio
async def test_graceful_skip_native_when_api_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a Workspace API is disabled, the connector exports the native
    types whose API IS enabled and skips only the disabled ones — instead
    of failing the whole sync or skipping every native type. Here Docs is
    enabled (exports) but Slides is disabled (skipped)."""
    listings = {
        "ROOT": [
            {
                "id": "doc2",
                "name": "Plain",
                "mimeType": NATIVE_DOC,
                "modifiedTime": "2026-05-10T00:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/doc2/edit",
            },
            {
                "id": "deck1",
                "name": "Deck",
                "mimeType": NATIVE_SLIDES,
                "modifiedTime": "2026-05-10T00:00:00Z",
                "webViewLink": "https://docs.google.com/presentation/d/deck1/edit",
            },
        ]
    }
    drive = _FakeDrive(_FakeFiles(listings, export_payloads={}, download_payloads={}))
    docs = _FakeDocs({
        "doc2": {"body": {"content": [
            {"paragraph": {"elements": [{"textRun": {"content": "kept doc", "textStyle": {}}}]}}
        ]}}
    })
    connector = GoogleDriveConnector()
    # Docs enabled, Slides disabled (Drive always on).
    _patch_services(
        connector, drive, docs, monkeypatch,
        api_status=WorkspaceApiStatus(
            drive=True, docs=True, sheets=True, slides=False, forms=True
        ),
    )

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.errors == []
    root = (tmp_path / "root").resolve()
    # Doc exported (API enabled)...
    assert (root / "Root" / "Plain.md").read_text().find("kept doc") >= 0
    # ...Slides skipped (API disabled) — no output, counted as skipped.
    assert not list(root.rglob("Deck*"))
    assert stats.files_skipped >= 1
    sidecar = json.loads((root / ".voitta_sources.json").read_text())
    assert sidecar["Root/Plain.md"]["url"].startswith("https://docs.google.com/document/d/doc2/edit")


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
            # ``vnd.google-apps.site`` has no registered exporter — the
            # connector must drop it silently.
            {
                "id": "site1",
                "name": "Wiki",
                "mimeType": "application/vnd.google-apps.site",
                "modifiedTime": "2026-01-01T00:00:00Z",
            },
            {
                "id": "f1",
                "name": "kept.dat",
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
    assert (root / "Root" / "kept.dat").exists()
    # The unsupported native type produced no on-disk file.
    assert not list(root.rglob("Wiki*"))


@pytest.mark.asyncio
async def test_files_only_skips_native_keeps_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """files_only=True: native Docs/Sheets/etc. are skipped entirely (no
    Workspace-API export), while ordinary binary files still download.
    This is the mode that lets sync run when the Docs/Sheets/Slides/Forms
    APIs are disabled in the GCP project."""
    listings = {
        "ROOT": [
            {
                "id": "doc1",
                "name": "Spec",
                "mimeType": NATIVE_DOC,  # has an exporter; would normally render
                "modifiedTime": "2026-01-01T00:00:00Z",
            },
            {
                "id": "f1",
                "name": "report.pdf",
                "mimeType": "application/pdf",
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
            download_payloads={"f1": b"pdf"},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        files_only=True,
    )
    root = (tmp_path / "root").resolve()
    # Binary file synced.
    assert (root / "Root" / "report.pdf").read_bytes() == b"pdf"
    # Native Doc skipped — no markdown export anywhere on disk.
    assert not list(root.rglob("Spec*"))
    assert stats.files_skipped >= 1


@pytest.mark.asyncio
async def test_mirror_deletes_locals_not_on_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = {
        "ROOT": [
            {
                "id": "f1",
                "name": "kept.dat",
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
    assert (root / "Root" / "kept.dat").exists()


@pytest.mark.asyncio
async def test_ignored_extensions_are_skipped_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Media + archive blobs should never be requested from Drive — the
    matcher fires before the download producer runs. Catches a regression
    where adding new globs to ``VOITTA_IGNORE_PATTERNS`` would still pull
    bytes over the wire because the connector applied the rule too late."""
    listings = {
        "ROOT": [
            {
                "id": "vid1",
                "name": "demo.mp4",  # ignored by *.mp4
                "mimeType": "video/mp4",
                "size": "999999",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "x",
            },
            {
                "id": "zip1",
                "name": "logs.tar.gz",  # ignored by *.tar.gz
                "mimeType": "application/gzip",
                "size": "999999",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "y",
            },
            {
                "id": "kept",
                "name": "notes.txt",  # not ignored
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "z",
            },
        ]
    }
    # Crucially: no download_payloads for the ignored ids. If the
    # connector tried to fetch them, _FakeFiles.get_media would KeyError.
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"kept": b"hello"},
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
    assert stats.files_skipped == 2
    root = (tmp_path / "root").resolve()
    assert (root / "Root" / "notes.txt").read_bytes() == b"hello"
    assert not (root / "Root" / "demo.mp4").exists()
    assert not (root / "Root" / "logs.tar.gz").exists()


@pytest.mark.asyncio
async def test_extensionless_recording_is_skipped_by_mime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive Meet recordings sometimes have no extension and titles like
    ``Recording 2026-04-12`` — the filename ignore globs can't see them,
    but Drive still tags them ``video/mp4``. The MIME blocklist must
    catch these before the download producer runs (otherwise a 200MB
    recording lands on disk for every team meeting)."""
    listings = {
        "ROOT": [
            {
                "id": "rec1",
                "name": "Recording 2026-04-12",  # no extension!
                "mimeType": "video/mp4",
                "size": "200000000",
                "modifiedTime": "2026-04-12T00:00:00Z",
                "md5Checksum": "v",
            },
            {
                "id": "voice1",
                "name": "voicememo-9347",  # no extension, audio
                "mimeType": "audio/mpeg",
                "size": "5000000",
                "modifiedTime": "2026-04-12T00:00:00Z",
                "md5Checksum": "a",
            },
            {
                "id": "kept",
                "name": "notes.txt",
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-04-12T00:00:00Z",
                "md5Checksum": "n",
            },
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            # No payload for the recordings — _FakeFiles.get_media would
            # KeyError if the connector tried to download them.
            download_payloads={"kept": b"hello"},
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
    assert stats.files_skipped == 2
    root = (tmp_path / "root").resolve()
    assert (root / "Root" / "notes.txt").read_bytes() == b"hello"
    assert not (root / "Root" / "Recording 2026-04-12").exists()
    assert not (root / "Root" / "voicememo-9347").exists()


@pytest.mark.asyncio
async def test_ignored_subfolder_is_not_recursed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subfolder named ``node_modules`` (or any other directory glob in
    ``ignore_patterns``) is skipped entirely — not enumerated, not
    descended into. Saves a recursive Drive listing on giant unhelpful
    trees."""
    listings = {
        "ROOT": [
            {
                "id": "nm",
                "name": "node_modules",  # matches the default ignore set
                "mimeType": NATIVE_FOLDER,
                "modifiedTime": "2026-01-01T00:00:00Z",
            },
            {
                "id": "kept",
                "name": "README.txt",
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "x",
            },
        ],
        # node_modules wouldn't normally be queried; if the test fails it
        # WILL be queried, so put a real-looking child here so we can
        # detect leakage if the recursion happened anyway.
        "nm": [
            {
                "id": "leaked",
                "name": "should-not-appear.txt",
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "y",
            },
        ],
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"kept": b"hello", "leaked": b"WRONG"},
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
    assert (root / "Root" / "README.txt").exists()
    # No node_modules dir, no leaked file anywhere under root.
    assert not list(root.rglob("node_modules"))
    assert not list(root.rglob("should-not-appear.txt"))


@pytest.mark.asyncio
async def test_multiple_folders_each_get_own_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two picked folders → two top-level subdirectories, no collisions."""
    listings = {
        "A": [
            {
                "id": "a1",
                "name": "shared.dat",
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
                "name": "shared.dat",
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
    assert (root / "Project Alpha" / "shared.dat").read_bytes() == b"AAA"
    assert (root / "Project Beta" / "shared.dat").read_bytes() == b"BBB"


@pytest.mark.asyncio
async def test_progress_callback_fires_through_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connector should walk the SPA's expected phase machine —
    connecting → listing → downloading → cleaning → done — and report a
    monotonically increasing ``done`` per phase. The user-facing badge
    depends on this ordering to render the right verb at each stage."""
    listings = {
        "ROOT": [
            {
                "id": f"f{i}",
                "name": f"f{i}.txt",
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": str(i),
            }
            for i in range(3)
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={f"f{i}": b"hello" for i in range(3)},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    captured: list[tuple[str, int, int, dict | None]] = []

    def _cb(phase: str, done: int, total: int, detail: dict | None) -> None:
        captured.append((phase, done, total, detail))

    await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        progress_cb=_cb,
    )

    phases = [p for (p, _, _, _) in captured]
    # Required phases land in order, with at least one ``done`` to clear
    # the SPA badge.
    for phase in ("connecting", "listing", "downloading", "cleaning", "done"):
        assert phase in phases, f"missing phase {phase!r} in {phases}"
    assert phases.index("connecting") < phases.index("listing")
    assert phases.index("listing") < phases.index("downloading")
    assert phases.index("downloading") < phases.index("cleaning")
    assert phases.index("cleaning") < phases.index("done")

    # Downloading reports each file (3 here, below the throttle) — the
    # final downloading event matches total.
    download_evts = [
        (d, t) for (p, d, t, _) in captured if p == "downloading"
    ]
    assert download_evts[-1] == (3, 3)

    # Listing emits include the rich ``detail`` breadcrumb the SPA needs
    # to animate the badge: current folder name + running items_seen
    # count. Without these the pill freezes on long enumerations.
    listing_details = [
        d for (p, _, _, d) in captured if p == "listing" and d is not None
    ]
    assert listing_details, "listing phase must emit detail dicts"
    assert listing_details[-1]["folders_done"] == 1
    assert listing_details[-1]["folders_total"] == 1
    assert listing_details[-1]["current_folder"] == "Root"
    # items_seen tracks across the listing — final value matches the
    # number of entries the connector queued for download.
    assert listing_details[-1]["items_seen"] == 3


@pytest.mark.asyncio
async def test_progress_callback_failures_dont_break_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flaky observer mustn't crash the sync. Files still land on disk
    even when every progress call raises."""
    listings = {
        "ROOT": [
            {
                "id": "f1",
                "name": "note.txt",
                "mimeType": "text/plain",
                "size": "5",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "md5Checksum": "x",
            }
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(listings, export_payloads={}, download_payloads={"f1": b"hello"})
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    def _bad(phase, done, total, detail):
        raise RuntimeError(f"observer broke on {phase}")

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        progress_cb=_bad,
    )
    assert stats.errors == []
    assert (tmp_path / "root" / "Root" / "note.txt").read_bytes() == b"hello"


def test_is_drive_404_recognises_httperror_404() -> None:
    """``_is_drive_404`` is the routing key between user-facing errors
    and the silent ``files_404`` bucket. False positives here would
    swallow real failures."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    from voitta_rag_enterprise.services.sync.google_drive import _is_drive_404

    def _http_error(status: int) -> HttpError:
        resp = Response({"status": status})
        resp.status = status
        return HttpError(resp, b'{"error": {"code": ' + str(status).encode() + b'}}')

    assert _is_drive_404(_http_error(404)) is True
    # Don't bucket other 4xx as 404 — those are real errors.
    assert _is_drive_404(_http_error(403)) is False
    assert _is_drive_404(_http_error(500)) is False
    # Random non-HttpError exceptions never look like 404s.
    assert _is_drive_404(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_404_on_download_routes_to_files_404_not_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: Drive lists a file then 404s on get_media. The
    failure must NOT land in ``stats.errors`` (which the SPA renders
    in the red error block) — it goes into ``stats.files_404`` so
    operators see one summary count instead of N error bullets."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    listings = {
        "ROOT": [
            {
                "id": "ghost",
                "name": "vanished.txt",
                "mimeType": "application/octet-stream",
                "size": "100",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "abc",
                "webViewLink": "https://drive.google.com/file/d/ghost/view",
            },
            {
                "id": "alive",
                "name": "kept.txt",
                "mimeType": "application/octet-stream",
                "size": "5",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "def",
                "webViewLink": "https://drive.google.com/file/d/alive/view",
            },
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"alive": b"hello"},
        )
    )

    # Wrap MediaIoBaseDownload to raise 404 on the ghost file. The
    # autouse fixture would otherwise install our happy-path fake; we
    # override it for this one test.
    class _SelectiveDownloader:
        def __init__(self, fh, request: _FakeMediaRequest) -> None:
            self._fh = fh
            self._req = request

        def next_chunk(self):
            if self._req._body == b"":
                resp = Response({"status": 404})
                resp.status = 404
                raise HttpError(resp, b'{"error": {"code": 404}}')
            if self._req._consumed:
                return None, True
            self._fh.write(self._req._body)
            self._req._consumed = True
            return None, True

    # Empty body for ``ghost`` triggers the 404 branch above.
    drive._files._download_payloads["ghost"] = b""

    import googleapiclient.http as _ghttp

    monkeypatch.setattr(_ghttp, "MediaIoBaseDownload", _SelectiveDownloader)

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.files_404 == 1, f"expected 1 404, got {stats.files_404}"
    # Critical: 404s must NOT bleed into stats.errors (which the SPA
    # surfaces as a red block in the sync modal).
    assert stats.errors == [], f"unexpected error entries: {stats.errors}"
    # The other file synced cleanly.
    assert (tmp_path / "root" / "Root" / "kept.txt").read_bytes() == b"hello"


def test_is_export_too_large_recognises_export_limit_403() -> None:
    """``_is_export_too_large`` routes Google's "This file is too large
    to be exported" 403 into the silent ``files_too_large`` bucket.
    False positives would swallow real permission errors."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    from voitta_rag_enterprise.services.sync.google_drive import _is_export_too_large

    def _http_error(status: int, reason: str = "") -> HttpError:
        resp = Response({"status": status})
        resp.status = status
        content = (
            b'{"error": {"errors": [{"reason": "%s"}], "message": "%s"}}'
            % (reason.encode(), reason.encode())
        )
        return HttpError(resp, content)

    assert _is_export_too_large(_http_error(403, "exportSizeLimitExceeded")) is True
    # Other 403 reasons are real permission errors — never bucketed.
    assert _is_export_too_large(_http_error(403, "insufficientPermissions")) is False
    # Same reason on a non-403 status doesn't match (defensive).
    assert _is_export_too_large(_http_error(429, "exportSizeLimitExceeded")) is False
    assert _is_export_too_large(_http_error(404)) is False
    assert _is_export_too_large(RuntimeError("boom")) is False


@pytest.mark.asyncio
async def test_export_too_large_routes_to_files_too_large_not_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a 403 ``exportSizeLimitExceeded`` from a producer
    (the Agnitio Drive's oversized workbook sidecar) must land in
    ``stats.files_too_large``, not ``stats.errors`` — the per-tab
    markdown already carries the content, so the missing sidecar is
    a summary count, not a red error bullet."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    listings = {
        "ROOT": [
            {
                "id": "huge",
                "name": "planning.xlsx",
                "mimeType": "application/octet-stream",
                "size": "99999999",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "abc",
                "webViewLink": "https://drive.google.com/file/d/huge/view",
            },
            {
                "id": "alive",
                "name": "kept.txt",
                "mimeType": "application/octet-stream",
                "size": "5",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "def",
                "webViewLink": "https://drive.google.com/file/d/alive/view",
            },
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"alive": b"hello", "huge": b""},
        )
    )

    class _SelectiveDownloader:
        def __init__(self, fh, request: _FakeMediaRequest) -> None:
            self._fh = fh
            self._req = request

        def next_chunk(self):
            if self._req._body == b"":
                resp = Response({"status": 403})
                resp.status = 403
                raise HttpError(
                    resp,
                    b'{"error": {"errors": [{"reason": "exportSizeLimitExceeded"}],'
                    b' "message": "This file is too large to be exported."}}',
                )
            if self._req._consumed:
                return None, True
            self._fh.write(self._req._body)
            self._req._consumed = True
            return None, True

    import googleapiclient.http as _ghttp

    monkeypatch.setattr(_ghttp, "MediaIoBaseDownload", _SelectiveDownloader)

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.files_too_large == 1, (
        f"expected 1 too-large skip, got {stats.files_too_large}"
    )
    assert stats.errors == [], f"unexpected error entries: {stats.errors}"
    # The other file synced cleanly.
    assert (tmp_path / "root" / "Root" / "kept.txt").read_bytes() == b"hello"


def test_folders_field_round_trip() -> None:
    from voitta_rag_enterprise.services.sync.google_drive import (
        coerce_folders_field,
        encode_folders_field,
    )

    # Empty / None → empty list
    assert coerce_folders_field(None) == []
    assert coerce_folders_field("") == []

    # JSON-array shape round-trips
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


# ---------------------------------------------------------------------------
# Regressions from the Sep 2026 Agnitio incident: two Drive roots had been
# unshared from the service account for ~4 months and synced 0 files in
# silence, while 3 permanently-undownloadable Chat attachments held the
# folder at sync_status='error' on every run. See docs/gdrive-sync-fixes.md.
# ---------------------------------------------------------------------------


def _http_err(status: int, reason: str = "", message: str = ""):
    """Build an HttpError the way googleapiclient hands one to us."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    resp = Response({"status": status})
    resp.status = status
    body = (
        b'{"error": {"errors": [{"reason": "%s"}], "message": "%s"}}'
        % (reason.encode(), (message or reason).encode())
    )
    return HttpError(resp, body)


def test_is_cannot_download_recognises_chat_attachment_403() -> None:
    """``cannotDownloadFile`` (Chat transcripts) buckets silently; every
    other 403 stays a real, user-visible error."""
    from voitta_rag_enterprise.services.sync.google_drive import _is_cannot_download

    assert _is_cannot_download(_http_err(403, "cannotDownloadFile")) is True
    assert _is_cannot_download(_http_err(403, "insufficientPermissions")) is False
    assert _is_cannot_download(_http_err(403, "exportSizeLimitExceeded")) is False
    assert _is_cannot_download(_http_err(404, "cannotDownloadFile")) is False
    assert _is_cannot_download(RuntimeError("boom")) is False


def test_is_root_inaccessible_only_matches_403_404() -> None:
    """Narrow on purpose: a transport blip or a stubbed client must never
    be mistaken for "this root is not shared with us"."""
    from voitta_rag_enterprise.services.sync.google_drive import _is_root_inaccessible

    assert _is_root_inaccessible(_http_err(404, "notFound")) is True
    assert _is_root_inaccessible(_http_err(403, "PERMISSION_DENIED")) is True
    assert _is_root_inaccessible(_http_err(500, "backendError")) is False
    assert _is_root_inaccessible(_http_err(429, "rateLimitExceeded")) is False
    assert _is_root_inaccessible(AttributeError("no attribute 'get'")) is False
    assert _is_root_inaccessible(RuntimeError("boom")) is False


class _FakeFilesWithGet(_FakeFiles):
    """``_FakeFiles`` plus the ``files().get`` that ``_fetch_shared_by``
    calls, so a root's metadata can be made readable or not."""

    def __init__(self, *args: Any, get_status: dict[str, int] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._get_status = get_status or {}

    def get(self, *, fileId: str, **kwargs: Any):
        status = self._get_status.get(fileId)
        if status is not None:
            raise _http_err(status, "notFound", "File not found.")
        return _FakeRequest({"ownedByMe": True})


@pytest.mark.asyncio
async def test_unshared_root_listing_zero_items_is_surfaced_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident itself: Drive answers "'<id>' in parents" for an
    unshared root with an empty page, not a 403 — so the only signal is
    the 404 from ``files.get``. Zero items AND unreadable metadata must
    become a visible error instead of silence."""
    drive = _FakeDrive(
        _FakeFilesWithGet(
            {"DEAD": [], "LIVE": [{
                "id": "alive",
                "name": "kept.txt",
                "mimeType": "application/octet-stream",
                "size": "5",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "def",
                "webViewLink": "https://drive.google.com/file/d/alive/view",
            }]},
            export_payloads={},
            download_payloads={"alive": b"hello"},
            get_status={"DEAD": 404},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[
            {"id": "DEAD", "name": "Meet Recordings"},
            {"id": "LIVE", "name": "Live Folder"},
        ],
    )
    assert len(stats.errors) == 1, f"expected 1 dead-root error, got {stats.errors}"
    assert "Meet Recordings" in stats.errors[0]
    assert "not" in stats.errors[0] and "shared" in stats.errors[0]
    # The reachable root still synced.
    assert (tmp_path / "root" / "Live Folder" / "kept.txt").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_empty_but_readable_root_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false-positive guard: the Agnitio Drive has an accessible root
    ('aclaw') that is simply empty. Readable metadata => stay quiet."""
    drive = _FakeDrive(
        _FakeFilesWithGet(
            {"EMPTY": []}, export_payloads={}, download_payloads={},
        )
    )
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "EMPTY", "name": "aclaw"}],
    )
    assert stats.errors == [], f"empty-but-readable root must stay quiet: {stats.errors}"


@pytest.mark.asyncio
async def test_cannot_download_routes_to_files_undownloadable_not_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Chat attachment's 403 ``cannotDownloadFile`` must count into
    ``files_undownloadable`` and leave ``stats.errors`` empty, so three of
    them stop pinning the folder to sync_status='error' forever."""
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    listings = {
        "ROOT": [
            {
                "id": "chat",
                "name": "Demo Sync - Chat",
                "mimeType": "application/octet-stream",
                "size": "100",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "abc",
                "webViewLink": "https://drive.google.com/file/d/chat/view",
            },
            {
                "id": "alive",
                "name": "kept.txt",
                "mimeType": "application/octet-stream",
                "size": "5",
                "modifiedTime": "2026-05-10T00:00:00Z",
                "md5Checksum": "def",
                "webViewLink": "https://drive.google.com/file/d/alive/view",
            },
        ]
    }
    drive = _FakeDrive(
        _FakeFiles(
            listings,
            export_payloads={},
            download_payloads={"alive": b"hello", "chat": b""},
        )
    )

    class _ChatRefusingDownloader:
        def __init__(self, fh, request: _FakeMediaRequest) -> None:
            self._fh = fh
            self._req = request

        def next_chunk(self):
            if self._req._body == b"":
                resp = Response({"status": 403})
                resp.status = 403
                raise HttpError(
                    resp,
                    b'{"error": {"errors": [{"reason": "cannotDownloadFile"}],'
                    b' "message": "This file cannot be downloaded by the user."}}',
                )
            if self._req._consumed:
                return None, True
            self._fh.write(self._req._body)
            self._req._consumed = True
            return None, True

    import googleapiclient.http as _ghttp

    monkeypatch.setattr(_ghttp, "MediaIoBaseDownload", _ChatRefusingDownloader)

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)

    stats = await connector.sync(
        folder_root=tmp_path / "root",
        auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.files_undownloadable == 1, (
        f"expected 1 undownloadable skip, got {stats.files_undownloadable}"
    )
    assert stats.errors == [], f"unexpected error entries: {stats.errors}"
    assert stats.as_dict()["files_undownloadable"] == 1
    assert (tmp_path / "root" / "Root" / "kept.txt").read_bytes() == b"hello"


# ---------------------------------------------------------------------------
# shared-with-me discovery. A file shared one-by-one is readable but
# unreachable by any folder walk (Drive grants the file, not its parent, and
# hides parents the caller can't see). See docs/gdrive-sync-fixes.md.
# ---------------------------------------------------------------------------


class _FakeFilesShared(_FakeFiles):
    """``_FakeFiles`` that also answers ``sharedWithMe=true`` queries.

    Mirrors the real API's shape: the shared set is keyed independently of
    the parent-based listings, because that is exactly the point — these
    files have no parent this credential can list.
    """

    def __init__(self, *args: Any, shared: list[dict[str, Any]] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._shared = shared or []

    def list(self, **kwargs):
        if "sharedWithMe" in kwargs.get("q", ""):
            return _FakeRequest({"files": self._shared})
        return super().list(**kwargs)


def _shared_doc(fid: str, name: str, md5: str = "aaa") -> dict[str, Any]:
    return {
        "id": fid,
        "name": name,
        "mimeType": "application/octet-stream",
        "size": "5",
        "modifiedTime": "2026-09-22T00:00:00Z",
        "md5Checksum": md5,
        "webViewLink": f"https://drive.google.com/file/d/{fid}/view",
        "sharingUser": {"displayName": "Gigi", "emailAddress": "gigi@agnitio.ai"},
    }


@pytest.mark.asyncio
async def test_shared_with_me_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in: without the flag the shared set is never even queried, so
    existing installs don't silently start ingesting unrelated documents."""
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": []}, export_payloads={}, download_payloads={"s1": b"hello"},
        shared=[_shared_doc("s1", "hand-shared.txt")],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
    )
    assert stats.files_added == 0
    assert not (tmp_path / "root" / "Shared with me").exists()


@pytest.mark.asyncio
async def test_shared_with_me_ingests_unreachable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident: a file that no ``'<id>' in parents`` query returns is
    picked up and lands under the synthetic directory."""
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": []}, export_payloads={}, download_payloads={"s1": b"hello"},
        shared=[_shared_doc("s1", "Triadex Checkin - Notes by Gemini.txt")],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    dest = tmp_path / "root" / "Shared with me" / "Triadex Checkin - Notes by Gemini.txt"
    assert dest.read_bytes() == b"hello", f"not materialised; errors={stats.errors}"
    assert stats.files_added == 1
    assert stats.errors == []


@pytest.mark.asyncio
async def test_shared_with_me_does_not_duplicate_folder_walked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file reachable BOTH ways keeps its real folder path — the shared
    pass skips ids the walk already saw, so it is not synced twice."""
    item = _shared_doc("dup", "kept.txt")
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": [item]}, export_payloads={}, download_payloads={"dup": b"hello"},
        shared=[item],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    assert (tmp_path / "root" / "Root" / "kept.txt").read_bytes() == b"hello"
    assert not (tmp_path / "root" / "Shared with me").exists(), (
        "folder-walked file must not be duplicated under the synthetic dir"
    )
    assert stats.files_added == 1


@pytest.mark.asyncio
async def test_shared_with_me_respects_ignore_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared pass goes through the same ``_process_item``, so ignore
    globs (the reason Meet .mp4 recordings stay out) still apply."""
    monkeypatch.setenv("VOITTA_IGNORE_PATTERNS", "*.mp4")
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": []}, export_payloads={},
        download_payloads={"vid": b"x", "ok": b"hello"},
        shared=[_shared_doc("vid", "Recording.mp4"), _shared_doc("ok", "notes.txt", "bbb")],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    shared_dir = tmp_path / "root" / "Shared with me"
    assert (shared_dir / "notes.txt").exists()
    assert not (shared_dir / "Recording.mp4").exists()
    assert stats.files_skipped >= 1


@pytest.mark.asyncio
async def test_shared_with_me_failure_keeps_folder_walk_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken shared pass is reported but must not discard what the
    folder walk already found."""
    class _Exploding(_FakeFilesShared):
        def list(self, **kwargs):
            if "sharedWithMe" in kwargs.get("q", ""):
                raise RuntimeError("drive is sulking")
            return _FakeFiles.list(self, **kwargs)

    drive = _FakeDrive(_Exploding(
        {"ROOT": [_shared_doc("ok", "kept.txt")]},
        export_payloads={}, download_payloads={"ok": b"hello"},
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    assert (tmp_path / "root" / "Root" / "kept.txt").read_bytes() == b"hello"
    assert len(stats.errors) == 1 and "drive is sulking" in stats.errors[0]


@pytest.mark.asyncio
async def test_shared_dir_name_cannot_collide_with_a_real_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A picked root genuinely named "Shared with me" keeps that dir; the
    synthetic one is suffixed via the same dedup the roots use."""
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": [_shared_doc("r1", "from-root.txt")]},
        export_payloads={},
        download_payloads={"r1": b"root", "s1": b"shared"},
        shared=[_shared_doc("s1", "from-shared.txt", "bbb")],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Shared with me"}],
        shared_with_me=True,
    )
    assert (tmp_path / "root" / "Shared with me" / "from-root.txt").exists()
    assert (tmp_path / "root" / "Shared with me-2" / "from-shared.txt").exists()


@pytest.mark.asyncio
async def test_shared_folder_that_is_a_picked_root_is_not_rewalked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the picked roots are themselves shared with the credential,
    so ``sharedWithMe=true`` returns them as folders. Recursing into one would
    re-add the entire corpus under the synthetic directory. Caught on the live
    Agnitio Drive, where 7 of the 24 shared items were the configured roots."""
    child = _shared_doc("c1", "kept.txt")
    root_as_shared_folder = {
        "id": "ROOT",
        "name": "Meet Recordings",
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": "2026-09-22T00:00:00Z",
        "webViewLink": "https://drive.google.com/drive/folders/ROOT",
    }
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": [child]}, export_payloads={},
        download_payloads={"c1": b"hello"},
        shared=[root_as_shared_folder],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Meet Recordings"}],
        shared_with_me=True,
    )
    assert (tmp_path / "root" / "Meet Recordings" / "kept.txt").read_bytes() == b"hello"
    assert not (tmp_path / "root" / "Shared with me").exists(), (
        "a shared folder must not be walked by the shared-with-me pass"
    )
    assert stats.files_added == 1, f"corpus duplicated: {stats.as_dict()}"


@pytest.mark.asyncio
async def test_shared_folders_are_skipped_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared folders are out of scope for this pass: they are reachable by a
    folder walk, so they belong in the picker as roots. Recursing here dragged
    in unrelated subtrees and hit Drive 500s that aborted the whole pass."""
    shared_child = _shared_doc("dup", "already-have.txt")
    fresh_child = _shared_doc("new", "brand-new.txt", "ccc")
    shared_folder = {
        "id": "SHARED_DIR",
        "name": "Some Shared Folder",
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": "2026-09-22T00:00:00Z",
        "webViewLink": "https://drive.google.com/drive/folders/SHARED_DIR",
    }
    drive = _FakeDrive(_FakeFilesShared(
        {"ROOT": [shared_child], "SHARED_DIR": [shared_child, fresh_child]},
        export_payloads={},
        download_payloads={"dup": b"hello", "new": b"fresh"},
        shared=[shared_folder],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    assert (tmp_path / "root" / "Root" / "already-have.txt").exists()
    # Nothing from inside the shared folder is pulled in — not the duplicate,
    # not even the file only reachable through it.
    assert not (tmp_path / "root" / "Shared with me").exists()


# ---------------------------------------------------------------------------
# Orphan cleanup must never delete files whose source failed to list.
# Job 39189 on the live Agnitio Drive: Drive answered HTTP 500 partway
# through the shared-with-me listing, expected_paths came back half-built,
# and phase 4 deleted 277 previously-synced files. See docs/.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_shared_pass_does_not_delete_its_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient error during the shared-with-me listing must not turn its
    previously-synced files into orphans."""
    class _Exploding(_FakeFilesShared):
        def list(self, **kwargs):
            if "sharedWithMe" in kwargs.get("q", ""):
                raise _http_err(500, "internalError", "Internal Error")
            return _FakeFiles.list(self, **kwargs)

    drive = _FakeDrive(_Exploding(
        {"ROOT": [_shared_doc("r1", "from-root.txt")]},
        export_payloads={}, download_payloads={"r1": b"root"},
    ))
    root = tmp_path / "root"
    # A file a previous successful shared pass had left behind.
    stale = root / "Shared with me" / "earlier-meeting.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("notes from a previous run")

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=root, auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    assert stale.exists(), (
        "file under a source that failed to list was deleted as an orphan"
    )
    assert stats.files_removed == 0, f"unexpected deletions: {stats.as_dict()}"
    assert any("Shared with me" in e for e in stats.errors)


@pytest.mark.asyncio
async def test_failed_root_listing_does_not_delete_that_roots_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same protection for the folder walk — this exposure pre-dates the
    shared-with-me pass."""
    class _ExplodingRoot(_FakeFilesShared):
        def list(self, **kwargs):
            if "'BOOM'" in kwargs.get("q", ""):
                raise _http_err(500, "internalError", "Internal Error")
            return _FakeFiles.list(self, **kwargs)

    drive = _FakeDrive(_ExplodingRoot(
        {"OK": [_shared_doc("o1", "fine.txt")], "BOOM": []},
        export_payloads={}, download_payloads={"o1": b"fine"},
    ))
    root = tmp_path / "root"
    stale = root / "Broken" / "previously-synced.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("still valid, we just could not list it")

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=root, auth=_make_auth(),
        drive_folders=[{"id": "OK", "name": "Fine"}, {"id": "BOOM", "name": "Broken"}],
    )
    assert stale.exists(), "failed root's files must survive cleanup"
    assert stats.files_removed == 0


@pytest.mark.asyncio
async def test_cleanup_still_removes_orphans_under_healthy_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must be surgical: a source that listed fine still gets its
    genuinely-deleted files cleaned up, even while another source is broken."""
    class _ExplodingRoot(_FakeFilesShared):
        def list(self, **kwargs):
            if "'BOOM'" in kwargs.get("q", ""):
                raise _http_err(500, "internalError", "Internal Error")
            return _FakeFiles.list(self, **kwargs)

    drive = _FakeDrive(_ExplodingRoot(
        {"OK": [_shared_doc("o1", "fine.txt")], "BOOM": []},
        export_payloads={}, download_payloads={"o1": b"fine"},
    ))
    root = tmp_path / "root"
    (root / "Fine").mkdir(parents=True)
    gone = root / "Fine" / "deleted-in-drive.md"
    gone.write_text("no longer on the remote")
    protected = root / "Broken" / "keep-me.md"
    protected.parent.mkdir(parents=True)
    protected.write_text("source failed to list")

    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=root, auth=_make_auth(),
        drive_folders=[{"id": "OK", "name": "Fine"}, {"id": "BOOM", "name": "Broken"}],
    )
    assert not gone.exists(), "healthy source's orphan should still be removed"
    assert protected.exists(), "broken source's file must be kept"
    assert stats.files_removed == 1


@pytest.mark.asyncio
async def test_shared_pass_survives_one_bad_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreadable shared file must not cost us the whole pass — that is
    what turned a single Drive 500 into 277 deleted files (job 39189)."""
    good = _shared_doc("good", "meeting-notes.txt")
    bad = _shared_doc("bad", "cursed.txt", "bbb")

    class _OneBadDownload(_FakeFilesShared):
        def get_media(self, *, fileId: str, **kwargs: Any):
            if fileId == "bad":
                raise _http_err(500, "internalError", "Internal Error")
            return super().get_media(fileId=fileId, **kwargs)

    drive = _FakeDrive(_OneBadDownload(
        {"ROOT": []}, export_payloads={},
        download_payloads={"good": b"hello", "bad": b""},
        shared=[bad, good],
    ))
    connector = GoogleDriveConnector()
    _patch_services(connector, drive, _FakeDocs({}), monkeypatch)
    stats = await connector.sync(
        folder_root=tmp_path / "root", auth=_make_auth(),
        drive_folders=[{"id": "ROOT", "name": "Root"}],
        shared_with_me=True,
    )
    # The healthy sibling still landed despite the bad one.
    assert (tmp_path / "root" / "Shared with me" / "meeting-notes.txt").exists(), (
        f"good item lost to a bad sibling; errors={stats.errors}"
    )
