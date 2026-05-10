"""Google Sheets (``vnd.google-apps.spreadsheet``) → per-sheet markdown
+ full workbook xlsx.

Layout produced for a workbook ``Q4 Plan`` with sheets ``Sales`` and
``Marketing``::

    Q4 Plan/
        01-Sales.md
        02-Marketing.md
    .voitta_workbooks/
        Q4 Plan.xlsx

Per-sheet markdown caps at the first :data:`MAX_ROWS_PER_SHEET` rows so a
large workbook doesn't blow up an embedder's token budget. The full
workbook is exported to ``.voitta_workbooks/<rel>.xlsx`` and excluded
from indexing via the ``.voitta_workbooks`` ignore glob; clients fetch
the bytes on demand through the ``voitta_rag_get_workbook`` MCP tool.

Cells longer than :data:`MAX_CELL_CHARS` are clipped with ``…[truncated]``
inline so a paragraph-shaped formula result doesn't dominate one chunk.
"""

from __future__ import annotations

import logging
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
    safe_filename,
)

logger = logging.getLogger(__name__)


# Public knobs — values land in module-level constants so a deployment
# that needs different limits can monkey-patch in a wrapper script.
# No env-var override yet; if needed we surface one through Settings.
MAX_ROWS_PER_SHEET = 100
MAX_CELL_CHARS = 500

XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
WORKBOOKS_DIR = ".voitta_workbooks"

# Inline fingerprint header — same shape DocumentExporter uses so the
# connector's unchanged-detection works on the markdown summaries too.
FINGERPRINT_PREFIX = "<!--voitta-fingerprint:"
FINGERPRINT_SUFFIX = "-->"


class SpreadsheetExporter(NativeDriveExporter):
    """Render a Google Sheet into one markdown file per sheet plus the
    full workbook (xlsx) under the sidecar dir."""

    mime_type = "application/vnd.google-apps.spreadsheet"

    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,
        ctx: ExportContext,
    ) -> list[RemoteEntry]:
        sheet_id = item["id"]
        modified_time = item.get("modifiedTime", "")
        web_url = item.get("webViewLink") or _sheet_view_url(sheet_id)

        # ``spreadsheets.get`` with a small fields mask gets us the sheet
        # list + properties without dragging the cell contents over the
        # wire. Cells are fetched per-sheet by the producers below.
        spreadsheet = (
            ctx.sheets()
            .spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
            )
            .execute()
        )
        sheets = spreadsheet.get("sheets") or []

        out: list[RemoteEntry] = []

        # Disambiguate sheets whose titles sanitise to the same filename.
        used_names: dict[str, int] = {}

        for index, s in enumerate(sheets, start=1):
            props = s.get("properties") or {}
            title = props.get("title") or f"Sheet{index}"
            grid_id = int(props.get("sheetId") or 0)
            row_total = int(
                (props.get("gridProperties") or {}).get("rowCount") or 0
            )

            base_safe = safe_filename(title)
            collision = used_names.get(base_safe, 0)
            used_names[base_safe] = collision + 1
            safe = base_safe if collision == 0 else f"{base_safe}__{collision + 1}"

            md_rel = f"{rel_no_ext}/{index:02d}-{safe}.md"
            md_url = web_url.split("?")[0].split("#")[0] + f"#gid={grid_id}"
            md_fingerprint = f"{modified_time}#sheet:{grid_id}"

            out.append(
                RemoteEntry(
                    rel_path=md_rel,
                    url=md_url,
                    fingerprint=md_fingerprint,
                    tab=title,
                    producer=_make_sheet_producer(
                        spreadsheet_id=sheet_id,
                        sheet_title=title,
                        row_total=row_total,
                        rel_no_ext=rel_no_ext,
                        fingerprint=md_fingerprint,
                    ),
                )
            )

        # Full workbook export under the sidecar dir.
        out.append(
            RemoteEntry(
                rel_path=f"{WORKBOOKS_DIR}/{rel_no_ext}.xlsx",
                url=web_url,
                fingerprint=f"{modified_time}#xlsx",
                tab=None,
                producer=_make_xlsx_producer(sheet_id),
            )
        )
        return out


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_sheet_markdown(
    *,
    sheet_title: str,
    rows: list[list[str]],
    row_total: int,
    rel_no_ext: str,
) -> str:
    """Render the rows of one sheet as markdown.

    First non-empty row becomes the header. Subsequent rows are body.
    Cells are escaped for pipe-table safety, truncated, and padded to
    a uniform column count so the table is well-formed.

    A footer is appended when the workbook has more rows than we
    fetched — so search results land on something explanatory rather
    than just trailing data.
    """
    lines: list[str] = [f"# {sheet_title}", ""]

    if not rows:
        lines.append("_(empty sheet)_")
        return "\n".join(lines)

    # Pad to a uniform column count so the markdown table parses cleanly
    # regardless of how ragged the source data is.
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        lines.append("_(empty sheet)_")
        return "\n".join(lines)

    norm: list[list[str]] = []
    for r in rows:
        cells = [_format_cell(c) for c in r]
        while len(cells) < width:
            cells.append("")
        norm.append(cells)

    header = norm[0]
    body = norm[1:]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in body:
        lines.append("| " + " | ".join(r) + " |")

    fetched = len(norm)
    if row_total and row_total > fetched:
        lines.append("")
        lines.append(
            f"_Note: showing first {fetched} of {row_total} rows. Full workbook "
            f"at `{WORKBOOKS_DIR}/{rel_no_ext}.xlsx`._"
        )
    return "\n".join(lines)


def _format_cell(value: Any) -> str:
    """Convert one cell value to a pipe-table-safe markdown string.

    The Sheets API returns formatted strings; we still defensively
    coerce non-strings (formula evaluations occasionally surface as
    ints/bools when the grid has an unset format). Newlines inside a
    cell collapse to a single space so the table stays one row per
    sheet row. Pipes get escaped.
    """
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - len("…[truncated]")] + "…[truncated]"
    return text.replace("|", "\\|")


# ---------------------------------------------------------------------------
# Producers
# ---------------------------------------------------------------------------


def _make_sheet_producer(
    *,
    spreadsheet_id: str,
    sheet_title: str,
    row_total: int,
    rel_no_ext: str,
    fingerprint: str,
) -> Callable[[Path, Any, ProducerContext], None]:
    """Producer that fetches the first :data:`MAX_ROWS_PER_SHEET` rows
    via Sheets API and writes the markdown table."""

    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        sheets = ctx.sheets()
        # ``A1:ZZ<N>`` covers any reasonable column count without
        # paging. Sheets accepts up to column ZZZ today; ZZ is 702
        # columns which is well past anything a user-facing sheet has.
        cell_range = f"'{_escape_a1_quotes(sheet_title)}'!A1:ZZ{MAX_ROWS_PER_SHEET}"
        resp = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=cell_range,
                valueRenderOption="FORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
            .execute()
        )
        rows = resp.get("values") or []
        body = render_sheet_markdown(
            sheet_title=sheet_title,
            rows=rows,
            row_total=row_total,
            rel_no_ext=rel_no_ext,
        )
        text = f"{FINGERPRINT_PREFIX}{fingerprint}{FINGERPRINT_SUFFIX}\n{body}"
        _atomic_write_text(dest, text)

    return _produce


def _make_xlsx_producer(spreadsheet_id: str) -> Callable[[Path, Any, ProducerContext], None]:
    """Producer that exports the full workbook as ``.xlsx`` via the
    Drive ``files.export_media`` endpoint and writes it under the
    sidecar dir.

    Uses ``MediaIoBaseDownload`` for parity with the rest of the
    connector (chunked streaming + atomic replace).
    """

    def _produce(dest: Path, drive: Any, ctx: ProducerContext) -> None:  # noqa: ARG001
        from googleapiclient.http import MediaIoBaseDownload

        tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
        try:
            with tmp.open("wb") as f:
                request = drive.files().export_media(
                    fileId=spreadsheet_id, mimeType=XLSX_MIME
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_a1_quotes(title: str) -> str:
    """Single quotes inside a Sheets A1 sheet name double up."""
    return title.replace("'", "''")


def _atomic_write_text(dest: Path, text: str) -> None:
    tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _sheet_view_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
