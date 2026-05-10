"""SpreadsheetExporter — per-sheet md + full workbook xlsx."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voitta_rag_enterprise.services.sync.google_workspace_exporters import (
    ExportContext,
    ProducerContext,
    get_default_registry,
)
from voitta_rag_enterprise.services.sync.google_workspace_exporters.sheets import (
    MAX_CELL_CHARS,
    MAX_ROWS_PER_SHEET,
    SpreadsheetExporter,
    WORKBOOKS_DIR,
    render_sheet_markdown,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def execute(self) -> dict:
        return self._payload


class _FakeSpreadsheets:
    def __init__(self, get_resp: dict, values_resps: dict[tuple[str, str], dict]) -> None:
        self._get_resp = get_resp
        self._values_resps = values_resps

    def get(self, *, spreadsheetId: str, fields: str = "") -> _FakeRequest:  # noqa: ARG002
        return _FakeRequest(self._get_resp)

    def values(self):
        outer = self

        class _Values:
            def get(
                self,
                *,
                spreadsheetId: str,
                range: str,  # noqa: A002
                valueRenderOption: str = "",
                dateTimeRenderOption: str = "",
            ) -> _FakeRequest:
                return _FakeRequest(outer._values_resps[(spreadsheetId, range)])

        return _Values()


class _FakeSheets:
    def __init__(self, get_resp: dict, values_resps: dict[tuple[str, str], dict]) -> None:
        self._spreadsheets = _FakeSpreadsheets(get_resp, values_resps)

    def spreadsheets(self):
        return self._spreadsheets


def _ctx(
    tmp_path: Path,
    *,
    get_resp: dict,
    values_resps: dict[tuple[str, str], dict] | None = None,
) -> ExportContext:
    return ExportContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: _FakeSheets(get_resp, values_resps or {}),
        slides=lambda: None,
        forms=lambda: None,
        drive_thread_local=lambda: None,
        access_token="fake",
    )


def _producer_ctx(
    tmp_path: Path,
    *,
    get_resp: dict | None = None,
    values_resps: dict[tuple[str, str], dict] | None = None,
) -> ProducerContext:
    sheets = _FakeSheets(get_resp or {}, values_resps or {})
    return ProducerContext(
        folder_root=tmp_path,
        docs=lambda: None,
        sheets=lambda: sheets,
        slides=lambda: None,
        forms=lambda: None,
        access_token="fake",
    )


def _drive_item(item_id: str, name: str, modified_time: str = "2026-05-10T00:00:00Z") -> dict:
    return {
        "id": item_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": modified_time,
        "webViewLink": f"https://docs.google.com/spreadsheets/d/{item_id}/edit",
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registry_dispatches_sheet_mime() -> None:
    r = get_default_registry()
    found = r.find("application/vnd.google-apps.spreadsheet")
    assert isinstance(found, SpreadsheetExporter)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_sheet_markdown_basic_table() -> None:
    md = render_sheet_markdown(
        sheet_title="Sales",
        rows=[["Region", "Q4"], ["EU", "1000"], ["US", "2000"]],
        row_total=3,
        rel_no_ext="Q4 Plan",
    )
    assert md.startswith("# Sales\n\n")
    assert "| Region | Q4 |" in md
    assert "| --- | --- |" in md
    assert "| EU | 1000 |" in md
    assert "| US | 2000 |" in md
    # No truncation footer when row_total ≤ fetched.
    assert "showing first" not in md


def test_render_sheet_markdown_pads_ragged_rows() -> None:
    md = render_sheet_markdown(
        sheet_title="X",
        rows=[["a", "b", "c"], ["d"], ["e", "f"]],
        row_total=3,
        rel_no_ext="X",
    )
    # Each row should have three columns; the missing cells become empty.
    assert "| d |  |  |" in md
    assert "| e | f |  |" in md


def test_render_sheet_markdown_truncates_large_cell() -> None:
    big = "x" * (MAX_CELL_CHARS + 100)
    md = render_sheet_markdown(
        sheet_title="X",
        rows=[["header"], [big]],
        row_total=2,
        rel_no_ext="X",
    )
    assert "[truncated]" in md
    # The truncated cell must not exceed MAX_CELL_CHARS.
    body_line = [
        line for line in md.splitlines() if line.startswith("| ") and "truncated" in line
    ][0]
    cell = body_line.strip("| ").rstrip("|").strip()
    assert len(cell) <= MAX_CELL_CHARS


def test_render_sheet_markdown_emits_truncation_footer_when_capped() -> None:
    rows = [["header"]] + [[f"r{i}"] for i in range(MAX_ROWS_PER_SHEET - 1)]
    md = render_sheet_markdown(
        sheet_title="Big",
        rows=rows,
        row_total=4217,
        rel_no_ext="Q4 Plan",
    )
    assert "showing first 100 of 4217 rows" in md
    assert f"`{WORKBOOKS_DIR}/Q4 Plan.xlsx`" in md


def test_render_sheet_markdown_handles_pipes_and_newlines() -> None:
    md = render_sheet_markdown(
        sheet_title="X",
        rows=[["a", "b"], ["pipe|here", "line1\nline2"]],
        row_total=2,
        rel_no_ext="X",
    )
    assert "pipe\\|here" in md
    # Newlines collapse to spaces inside cells so the table stays one-row-per-line.
    assert "line1 line2" in md
    assert "line1\nline2" not in md.split("| ---")[1]


def test_render_sheet_markdown_empty_rows() -> None:
    md = render_sheet_markdown(
        sheet_title="Blank",
        rows=[],
        row_total=0,
        rel_no_ext="X",
    )
    assert "_(empty sheet)_" in md
    assert "|" not in md  # no table built


# ---------------------------------------------------------------------------
# Exporter — entries shape
# ---------------------------------------------------------------------------


def test_export_returns_one_md_per_sheet_plus_workbook(tmp_path: Path) -> None:
    get_resp = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "Sales",
                    "gridProperties": {"rowCount": 50, "columnCount": 3},
                }
            },
            {
                "properties": {
                    "sheetId": 12,
                    "title": "Marketing",
                    "gridProperties": {"rowCount": 200, "columnCount": 3},
                }
            },
        ]
    }
    entries = SpreadsheetExporter().export(
        _drive_item("ssid", "Q4 Plan"),
        "MyFolder/Q4 Plan",
        _ctx(tmp_path, get_resp=get_resp),
    )
    md_paths = sorted(e.rel_path for e in entries if e.rel_path.endswith(".md"))
    assert md_paths == [
        "MyFolder/Q4 Plan/01-Sales.md",
        "MyFolder/Q4 Plan/02-Marketing.md",
    ]
    xlsx = next(e for e in entries if e.rel_path.endswith(".xlsx"))
    assert xlsx.rel_path == f"{WORKBOOKS_DIR}/MyFolder/Q4 Plan.xlsx"
    # gid query in source_url maps to the right sheet.
    sales = next(e for e in entries if "01-Sales.md" in e.rel_path)
    marketing = next(e for e in entries if "02-Marketing.md" in e.rel_path)
    assert "#gid=0" in sales.url
    assert "#gid=12" in marketing.url


def test_export_disambiguates_collision_in_safe_filename(tmp_path: Path) -> None:
    """Two sheets whose titles sanitize to the same name get suffixed."""
    get_resp = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 1,
                    "title": "A/B",
                    "gridProperties": {"rowCount": 1, "columnCount": 1},
                }
            },
            {
                "properties": {
                    "sheetId": 2,
                    "title": "A:B",
                    "gridProperties": {"rowCount": 1, "columnCount": 1},
                }
            },
        ]
    }
    entries = SpreadsheetExporter().export(
        _drive_item("ssid", "x"),
        "x",
        _ctx(tmp_path, get_resp=get_resp),
    )
    md_paths = sorted(e.rel_path for e in entries if e.rel_path.endswith(".md"))
    # Both sanitize to "A-B"; second one becomes "A-B__2".
    assert md_paths == ["x/01-A-B.md", "x/02-A-B__2.md"]


def test_per_sheet_fingerprint_changes_with_modified_time(tmp_path: Path) -> None:
    get_resp = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 7,
                    "title": "S",
                    "gridProperties": {"rowCount": 1, "columnCount": 1},
                }
            }
        ]
    }
    e1 = SpreadsheetExporter().export(
        _drive_item("id", "n", modified_time="2026-05-10T00:00:00Z"),
        "n",
        _ctx(tmp_path, get_resp=get_resp),
    )[0]
    e2 = SpreadsheetExporter().export(
        _drive_item("id", "n", modified_time="2026-05-10T00:01:00Z"),
        "n",
        _ctx(tmp_path, get_resp=get_resp),
    )[0]
    assert e1.fingerprint != e2.fingerprint
    assert "#sheet:7" in e1.fingerprint


def test_export_empty_workbook_still_emits_xlsx(tmp_path: Path) -> None:
    """A workbook with zero sheets is unusual but valid; the xlsx export
    still happens so the user can grab a copy."""
    entries = SpreadsheetExporter().export(
        _drive_item("ssid", "Empty"),
        "Empty",
        _ctx(tmp_path, get_resp={"sheets": []}),
    )
    md_entries = [e for e in entries if e.rel_path.endswith(".md")]
    xlsx_entries = [e for e in entries if e.rel_path.endswith(".xlsx")]
    assert md_entries == []
    assert len(xlsx_entries) == 1


# ---------------------------------------------------------------------------
# Producers
# ---------------------------------------------------------------------------


def test_sheet_producer_writes_with_fingerprint_header_and_table(tmp_path: Path) -> None:
    get_resp = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "S",
                    "gridProperties": {"rowCount": 2, "columnCount": 2},
                }
            }
        ]
    }
    values_resps = {
        ("ssid", "'S'!A1:ZZ100"): {
            "values": [["h1", "h2"], ["a", "b"]],
        }
    }
    entries = SpreadsheetExporter().export(
        _drive_item("ssid", "Wb"),
        "Wb",
        _ctx(tmp_path, get_resp=get_resp, values_resps=values_resps),
    )
    md = next(e for e in entries if e.rel_path.endswith(".md"))
    dest = tmp_path / md.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    md.producer(
        dest,
        drive=None,
        ctx=_producer_ctx(tmp_path, get_resp=get_resp, values_resps=values_resps),
    )
    text = dest.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("<!--voitta-fingerprint:")
    assert "| h1 | h2 |" in text
    assert "| a | b |" in text


def test_sheet_producer_handles_titles_with_quotes(tmp_path: Path) -> None:
    """Sheets titled like ``Mike's tab`` need their A1 reference escaped."""
    get_resp = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "Mike's tab",
                    "gridProperties": {"rowCount": 1, "columnCount": 1},
                }
            }
        ]
    }
    values_resps = {
        ("ssid", "'Mike''s tab'!A1:ZZ100"): {"values": [["x"]]},
    }
    entries = SpreadsheetExporter().export(
        _drive_item("ssid", "Wb"),
        "Wb",
        _ctx(tmp_path, get_resp=get_resp, values_resps=values_resps),
    )
    md = next(e for e in entries if e.rel_path.endswith(".md"))
    dest = tmp_path / md.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    md.producer(
        dest,
        drive=None,
        ctx=_producer_ctx(tmp_path, get_resp=get_resp, values_resps=values_resps),
    )
    assert "x" in dest.read_text()


def test_xlsx_producer_streams_via_export_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full-workbook producer uses Drive's ``files.export_media``
    + ``MediaIoBaseDownload``; we patch the downloader to a one-shot."""

    class _OneShotDownloader:
        def __init__(self, fh, request: Any) -> None:
            self._fh = fh
            self._request = request
            self._done = False

        def next_chunk(self):
            if self._done:
                return None, True
            self._fh.write(b"XLSX-BYTES")
            self._done = True
            return None, True

    import googleapiclient.http

    monkeypatch.setattr(googleapiclient.http, "MediaIoBaseDownload", _OneShotDownloader)

    class _FakeMedia:
        pass

    class _FakeFiles:
        def export_media(self, *, fileId: str, mimeType: str) -> _FakeMedia:  # noqa: ARG002
            return _FakeMedia()

    class _FakeDrive:
        def files(self):
            return _FakeFiles()

    get_resp = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "S",
                    "gridProperties": {"rowCount": 1, "columnCount": 1},
                }
            }
        ]
    }
    entries = SpreadsheetExporter().export(
        _drive_item("ssid", "Wb"),
        "Wb",
        _ctx(tmp_path, get_resp=get_resp),
    )
    xlsx = next(e for e in entries if e.rel_path.endswith(".xlsx"))
    dest = tmp_path / xlsx.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    xlsx.producer(dest, drive=_FakeDrive(), ctx=_producer_ctx(tmp_path))
    assert dest.read_bytes() == b"XLSX-BYTES"
    # Sidecar location: under WORKBOOKS_DIR.
    assert dest.is_relative_to(tmp_path / WORKBOOKS_DIR)
