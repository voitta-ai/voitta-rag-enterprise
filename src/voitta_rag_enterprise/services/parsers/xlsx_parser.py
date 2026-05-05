"""XLSX parser via openpyxl.

Walks every worksheet (skipping hidden ones) and emits one markdown section
per sheet. The ``## Sheet: <name>`` heading is the natural chunk boundary
that downstream chunking already prefers.

Per-chunk sheet attribution would require threading metadata through the
chunker; out of scope for v1. For files synced from Google Drive the user
gets sheet names in the markdown headings, and the file's Drive URL in the
chunk payload.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .base import BaseParser, ParserResult

logger = logging.getLogger(__name__)

# Cap row/column scans on absurdly large sheets so a runaway export doesn't
# DoS the indexer. 50k rows by 64 cols of typical text is a few MB of markdown,
# which is more than the chunker would ever index meaningfully anyway.
_MAX_ROWS = 50_000
_MAX_COLS = 64


class XlsxParser(BaseParser):
    extensions: ClassVar[list[str]] = [".xlsx", ".xlsm"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            wb = load_workbook(
                str(file_path),
                read_only=True,
                data_only=True,  # use cached formula results, not "=A1+B1"
            )
        except Exception as e:
            return ParserResult.failure(f"xlsx open failed: {e}")

        sections: list[str] = []
        try:
            for sheet in wb.worksheets:
                if sheet.sheet_state != "visible":
                    continue
                rendered = _render_sheet(sheet)
                if rendered.strip():
                    sections.append(f"## Sheet: {sheet.title}\n\n{rendered}")
        finally:
            wb.close()

        return ParserResult(content="\n\n".join(sections))


def _render_sheet(sheet: Worksheet) -> str:
    """Render a worksheet as a pipe-style markdown table.

    Trims trailing blank rows/columns so a sheet with five real rows but
    1,000 empty placeholders doesn't drown the chunker. The first row is
    treated as a header — that's the convention almost every spreadsheet
    follows and gives the downstream model a free signal about column
    semantics.
    """
    rows: list[list[str]] = []
    for row_index, raw in enumerate(sheet.iter_rows(values_only=True)):
        if row_index >= _MAX_ROWS:
            break
        row = [_cell_to_str(c) for c in raw[:_MAX_COLS]]
        rows.append(row)

    rows = _trim_trailing_blank(rows)
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    for r in rows:
        while len(r) < width:
            r.append("")

    # Trim trailing blank columns (a header row with empty cells wastes width).
    while width > 1 and all(not r[width - 1] for r in rows):
        width -= 1
        for r in rows:
            r.pop()
    if width == 0:
        return ""

    header = rows[0]
    body = rows[1:]
    lines = ["| " + " | ".join(_md_escape(c) for c in header) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in body:
        lines.append("| " + " | ".join(_md_escape(c) for c in r) + " |")
    return "\n".join(lines)


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _md_escape(text: str) -> str:
    """Pipe-tables are line-based; escape pipes and collapse newlines."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _trim_trailing_blank(rows: list[list[str]]) -> list[list[str]]:
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    return rows
