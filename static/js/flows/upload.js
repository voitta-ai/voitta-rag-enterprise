// Multi-file / multi-folder upload flow.
//
// Three entry points, all funnel through ``runUpload``:
//
//   * Upload files button (#btn-upload) → plain file picker, no folder
//     structure (each file lands directly under the currently-selected
//     subdirectory).
//   * Upload folder button (#btn-upload-folder) → picks one folder and
//     uploads its whole subtree. The top folder name is preserved as
//     part of each file's target rel_dir.
//   * Drag-and-drop on the file list (#browser-content) → arbitrary
//     mix of files and folders, traversed recursively. Multiple
//     folders can be dropped together; each retains its own subtree.
//
// Concurrency-limited (3 in-flight) so a 200-file drop doesn't pin
// the network. Per-file progress lines stay visible until everything
// settles, then auto-clear after 1.5s (5s on failure so the user can
// read the error).

import { api } from "../api.js";
import { getSelectedFolderId, getSelectedRelDir } from "./selection.js";

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

function joinRelDir(base, sub) {
    const b = (base || "").replace(/^\/+|\/+$/g, "");
    const s = (sub || "").replace(/^\/+|\/+$/g, "");
    if (!b) return s;
    if (!s) return b;
    return `${b}/${s}`;
}

function dirnameOf(relPath) {
    if (!relPath) return "";
    const idx = relPath.lastIndexOf("/");
    return idx < 0 ? "" : relPath.slice(0, idx);
}

// Build the per-file ``{file, relDir}`` entry from a browser File whose
// ``webkitRelativePath`` may carry a nested path. ``selectedRelDir`` is
// the user's current drill-down inside the managed folder; we prepend
// it so a drop targets the visible subtree.
function entryFromFile(file, selectedRelDir) {
    const rel = file.webkitRelativePath || "";
    const subDir = rel ? dirnameOf(rel) : "";
    return { file, relDir: joinRelDir(selectedRelDir, subDir) };
}

// ---------------------------------------------------------------------------
// DataTransferItem traversal — drag-and-drop path.
// ---------------------------------------------------------------------------
//
// ``DataTransferItem.webkitGetAsEntry()`` gives us a FileSystemEntry per
// dropped item (file or directory). We walk directory entries
// recursively, accumulating ``{file, relDir}`` entries where ``relDir``
// is the full path from the drop root including the directory's own
// name. Multiple folders dropped together each keep their own root.
//
// FileSystemDirectoryReader.readEntries() returns at most ~100 entries
// per call in Chromium; we loop until it returns an empty batch. Chrome
// docs: https://developer.mozilla.org/.../FileSystemDirectoryReader/readEntries.

async function readAllEntries(reader) {
    const out = [];
    while (true) {
        const batch = await new Promise((resolve, reject) =>
            reader.readEntries(resolve, reject)
        );
        if (!batch.length) return out;
        out.push(...batch);
    }
}

async function walkEntry(entry, pathPrefix, selectedRelDir, out) {
    // ``pathPrefix`` is the relative path from the drop's virtual root
    // to this entry's *parent* — empty string for top-level dropped
    // items, ``"foo"`` for items inside a dropped folder ``foo``, etc.
    if (entry.isFile) {
        const file = await new Promise((resolve, reject) =>
            entry.file(resolve, reject)
        );
        out.push({ file, relDir: joinRelDir(selectedRelDir, pathPrefix) });
        return;
    }
    if (entry.isDirectory) {
        const reader = entry.createReader();
        const children = await readAllEntries(reader);
        const nextPrefix = joinRelDir(pathPrefix, entry.name);
        for (const child of children) {
            await walkEntry(child, nextPrefix, selectedRelDir, out);
        }
    }
}

async function collectFromDataTransfer(dt, selectedRelDir) {
    const out = [];
    const items = Array.from(dt.items || []);
    // DataTransferItemList — webkitGetAsEntry exposes folders. If the
    // browser doesn't support it (very old), fall back to a flat file
    // list with no structure.
    if (items.length && typeof items[0].webkitGetAsEntry === "function") {
        const entries = items
            .map((it) => it.webkitGetAsEntry())
            .filter((e) => e != null);
        for (const entry of entries) {
            await walkEntry(entry, "", selectedRelDir, out);
        }
        return out;
    }
    for (const f of Array.from(dt.files || [])) {
        out.push(entryFromFile(f, selectedRelDir));
    }
    return out;
}

// ---------------------------------------------------------------------------
// Core upload runner
// ---------------------------------------------------------------------------

async function runUpload(entries) {
    if (!entries.length || !getSelectedFolderId()) return;

    const wrap = $("#upload-progress");
    const fill = $("#upload-progress-fill");
    const label = $("#upload-progress-label");
    const list = $("#upload-file-list");

    // Per-file row: name (with optional rel_dir prefix) + percent + state.
    const rows = entries.map(({ file, relDir }) => {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "name";
        // Show the per-file target path so the user can confirm the
        // folder structure preservation worked. ``relDir`` already
        // includes the user's current drill-down; strip that prefix
        // for display to keep rows compact.
        const selectedDir = getSelectedRelDir() || "";
        const displayDir = selectedDir && relDir.startsWith(selectedDir)
            ? relDir.slice(selectedDir.length).replace(/^\/+/, "")
            : relDir;
        name.textContent = displayDir
            ? `${displayDir}/${file.name}`
            : file.name;
        const pct = document.createElement("span");
        pct.className = "pct";
        pct.textContent = "0%";
        li.append(name, pct);
        list.append(li);
        return { file, relDir, li, pct, loaded: 0 };
    });
    const totalBytes = entries.reduce((sum, e) => sum + (e.file.size || 0), 0);
    wrap.hidden = false;
    fill.style.width = "0%";
    label.textContent = `Uploading ${entries.length} file(s)…`;

    function refreshAggregate() {
        const loaded = rows.reduce((sum, r) => sum + r.loaded, 0);
        const overall = totalBytes ? Math.round((loaded / totalBytes) * 100) : 0;
        fill.style.width = `${overall}%`;
        const remaining = rows.filter((r) => !r.li.classList.contains("done")
                                          && !r.li.classList.contains("failed")).length;
        const done = rows.length - remaining;
        label.textContent = remaining === 0
            ? "Done"
            : `Uploading ${done}/${rows.length} — ${overall}%`;
    }

    try {
        const { failures } = await api.uploadBatch(
            getSelectedFolderId(),
            entries,                       // per-entry relDir wins
            getSelectedRelDir(),           // fallback for plain File entries
            {
                concurrency: 3,
                onFileProgress: (idx, _file, p) => {
                    rows[idx].loaded = p.loaded;
                    rows[idx].pct.textContent = `${Math.round(p.fraction * 100)}%`;
                    refreshAggregate();
                },
                onFileDone: (idx, file) => {
                    rows[idx].loaded = file.size || rows[idx].loaded;
                    rows[idx].li.classList.add("done");
                    rows[idx].pct.textContent = "✓";
                    refreshAggregate();
                },
                onFileError: (idx, _file, err) => {
                    rows[idx].li.classList.add("failed");
                    rows[idx].pct.textContent = "×";
                    rows[idx].li.title = err.message;
                    refreshAggregate();
                },
            },
        );
        if (failures.length) {
            label.textContent = `Done — ${failures.length} failed`;
        }
        setTimeout(() => {
            wrap.hidden = true;
            list.replaceChildren();
        }, failures.length ? 5000 : 1500);
    } catch (err) {
        wrap.hidden = true;
        list.replaceChildren();
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// Wire entry points
// ---------------------------------------------------------------------------

$("#btn-upload").addEventListener("click", () => $("#upload-input").click());
$("#btn-upload-folder").addEventListener("click", () =>
    $("#upload-folder-input").click()
);

$("#upload-input").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files);
    const sel = getSelectedRelDir() || "";
    // Plain file picker — no folder structure to preserve.
    const entries = files.map((f) => ({ file: f, relDir: sel }));
    try {
        await runUpload(entries);
    } finally {
        e.target.value = "";
    }
});

$("#upload-folder-input").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files);
    const sel = getSelectedRelDir() || "";
    // Directory picker — each file has webkitRelativePath like
    // ``Foo/bar/baz.pdf``. The top folder name (``Foo``) is preserved
    // as the first segment of the rel_dir.
    const entries = files.map((f) => entryFromFile(f, sel));
    try {
        await runUpload(entries);
    } finally {
        e.target.value = "";
    }
});

// Drag-and-drop on the file browser pane. Supports multiple folders +
// files dropped together, each retaining its own subtree.
const dropTarget = document.querySelector(".browser-layout") || document.body;
let dragDepth = 0;

dropTarget.addEventListener("dragenter", (e) => {
    if (!getSelectedFolderId()) return;
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragDepth++;
    dropTarget.classList.add("drop-active");
});
dropTarget.addEventListener("dragover", (e) => {
    if (!getSelectedFolderId()) return;
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
});
dropTarget.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropTarget.classList.remove("drop-active");
});
dropTarget.addEventListener("drop", async (e) => {
    if (!getSelectedFolderId()) return;
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragDepth = 0;
    dropTarget.classList.remove("drop-active");
    const sel = getSelectedRelDir() || "";
    const entries = await collectFromDataTransfer(e.dataTransfer, sel);
    if (entries.length) await runUpload(entries);
});
