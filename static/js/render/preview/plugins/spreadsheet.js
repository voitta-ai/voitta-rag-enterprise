// Preview plugin: spreadsheets (XLSX, XLSM, XLS, ODS).
//
// Renders the workbook as an Excel-style grid: column letters along
// the top, row numbers down the left, sheet tabs to switch between
// worksheets. SheetJS is lazy-loaded from CDN on first preview and
// reused for subsequent ones.
//
// Caps: the backend already limits the rows/cols the parser sees for
// indexing, but here we read the raw file — so apply a render cap to
// avoid building a 100k-row DOM table.

import { registerPlugin } from "../index.js";

const SHEET_EXTS = new Set([".xlsx", ".xlsm", ".xls", ".ods"]);

const MAX_ROWS = 1000;
const MAX_COLS = 60;

let _XLSX = null;
let _abortCtrl = null;

const plugin = {
    canPreview(file) {
        return SHEET_EXTS.has(_ext(file.rel_path));
    },

    async mount(container, file) {
        container.classList.add("preview-spreadsheet");
        container.innerHTML = '<p class="preview-loading">Loading workbook…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        try {
            const [resp] = await Promise.all([
                fetch(`/api/files/${file.id}/raw`, { credentials: "same-origin", signal }),
                _XLSX ? Promise.resolve() : _loadSheetJs(),
            ]);
            if (signal.aborted) return;
            if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
            const buffer = await resp.arrayBuffer();
            if (signal.aborted) return;

            const wb = _XLSX.read(buffer, { type: "array", cellDates: true });
            if (!wb.SheetNames.length) {
                container.innerHTML = '<p class="preview-hint">Workbook has no sheets.</p>';
                return;
            }

            container.innerHTML = "";
            _renderWorkbook(container, wb);
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        container.classList.remove("preview-spreadsheet");
        container.innerHTML = "";
    },
};

async function _loadSheetJs() {
    _XLSX = await import("/static/js/vendor/xlsx.js");
}

function _renderWorkbook(container, wb) {
    // Tab strip at the top — Excel puts tabs at the bottom, but the
    // preview pane scrolls vertically, so the tabs would disappear off
    // the bottom on long sheets. Top keeps them always visible.
    const tabStrip = document.createElement("div");
    tabStrip.className = "preview-sheet-tabs";
    const grid = document.createElement("div");
    grid.className = "preview-sheet-scroll";

    const tabs = [];
    for (const name of wb.SheetNames) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "preview-sheet-tab";
        btn.textContent = name;
        btn.addEventListener("click", () => {
            for (const t of tabs) t.classList.toggle("active", t === btn);
            grid.innerHTML = "";
            grid.append(_renderSheet(wb.Sheets[name]));
        });
        tabs.push(btn);
        tabStrip.append(btn);
    }
    // Hide the tab strip for single-sheet workbooks — it adds nothing.
    if (wb.SheetNames.length === 1) tabStrip.hidden = true;

    container.append(tabStrip, grid);

    // Activate the first sheet.
    tabs[0].classList.add("active");
    grid.append(_renderSheet(wb.Sheets[wb.SheetNames[0]]));
}

function _renderSheet(sheet) {
    const wrap = document.createElement("div");
    wrap.className = "preview-sheet-wrap";

    const ref = sheet["!ref"];
    if (!ref) {
        wrap.innerHTML = '<p class="preview-hint">Empty sheet.</p>';
        return wrap;
    }
    const range = _XLSX.utils.decode_range(ref);
    const startRow = range.s.r;
    const startCol = range.s.c;
    const endRow = Math.min(range.e.r, startRow + MAX_ROWS - 1);
    const endCol = Math.min(range.e.c, startCol + MAX_COLS - 1);

    const truncatedRows = range.e.r - endRow;
    const truncatedCols = range.e.c - endCol;

    const table = document.createElement("table");
    table.className = "preview-sheet-grid";

    // Header: blank corner + column letters.
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const corner = document.createElement("th");
    corner.className = "preview-sheet-corner";
    headRow.append(corner);
    for (let c = startCol; c <= endCol; c++) {
        const th = document.createElement("th");
        th.className = "preview-sheet-col-head";
        th.textContent = _XLSX.utils.encode_col(c);
        headRow.append(th);
    }
    thead.append(headRow);
    table.append(thead);

    const tbody = document.createElement("tbody");
    for (let r = startRow; r <= endRow; r++) {
        const tr = document.createElement("tr");
        const rowHead = document.createElement("th");
        rowHead.className = "preview-sheet-row-head";
        rowHead.textContent = String(r + 1);
        tr.append(rowHead);
        for (let c = startCol; c <= endCol; c++) {
            const addr = _XLSX.utils.encode_cell({ r, c });
            const cell = sheet[addr];
            const td = document.createElement("td");
            if (cell) {
                // ``w`` is the formatted display string (correctly
                // rendered dates, currencies, percentages). ``v`` is
                // the raw value — fall back to it for cells SheetJS
                // hasn't formatted (rare).
                td.textContent = cell.w !== undefined
                    ? cell.w
                    : (cell.v !== undefined ? String(cell.v) : "");
                if (typeof cell.v === "number") td.classList.add("num");
                if (cell.v instanceof Date) td.classList.add("date");
            }
            tr.append(td);
        }
        tbody.append(tr);
    }
    table.append(tbody);
    wrap.append(table);

    if (truncatedRows > 0 || truncatedCols > 0) {
        const note = document.createElement("p");
        note.className = "preview-hint preview-sheet-trunc";
        const bits = [];
        if (truncatedRows > 0) bits.push(`${truncatedRows.toLocaleString()} more rows`);
        if (truncatedCols > 0) bits.push(`${truncatedCols} more columns`);
        note.textContent = `Showing first ${endRow - startRow + 1} rows × ${endCol - startCol + 1} cols (${bits.join(", ")} not shown).`;
        wrap.append(note);
    }

    return wrap;
}

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
