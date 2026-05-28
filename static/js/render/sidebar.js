// Sidebar Details pane: per-folder counts, badges, ext-table.
//
// Reads:
// - selection (which folder + subdir is highlighted)
// - files store (per-state counts inside the subtree)
// - folderStats store (chunk / image / byte totals + by-extension)
// - reindexProgress / syncProgress (pill state)
//
// Writes: only DOM under #folder-detail / #sidebar-empty / #ext-table.
// First-load REST fetch for stats lives here too via ``ensureFolderStats``;
// subsequent updates flow over folder.stats_changed and the store
// subscriber re-renders.

import { api } from "../api.js";
import { reconcileChildren, setIfChanged } from "../dom/reconcile.js";
import { getSelectedArtifactPage, getSelectedFileId, getSelectedFolderId, getSelectedRelDir } from "../flows/selection.js";
import { renderFilePreview, unmountPreview } from "./preview/index.js";
import { files, folders, folderStats, reindexProgress, syncProgress } from "../store.js";

const $ = (sel) => document.querySelector(sel);

export function renderSidebar() {
    const fileId = getSelectedFileId();
    if (fileId !== null) {
        const page = getSelectedArtifactPage();
        renderFilePreview(fileId, page != null ? { page } : {});
        return;
    }

    // No file selected — ensure any mounted preview is torn down.
    unmountPreview();
    const preview = $("#file-preview");
    if (preview) preview.hidden = true;

    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    const empty = $("#sidebar-empty");
    const detail = $("#folder-detail");

    if (!folder) {
        empty.hidden = false;
        detail.hidden = true;
        return;
    }

    empty.hidden = true;
    detail.hidden = false;

    const displayName = getSelectedRelDir()
        ? `${folder.display_name}/${getSelectedRelDir()}`
        : folder.display_name;
    $("#folder-name").textContent = displayName;
    $("#folder-path").textContent = getSelectedRelDir()
        ? `${folder.path}/${getSelectedRelDir()}`
        : folder.path;
    $("#folder-source-badge").textContent = folder.source_type;

    // Subtree-scoped counts (fall back to whole folder when relDir is empty).
    const allFolderFiles = files.get().filter((x) => x.folder_id === folder.id && x.state !== "deleted");
    const subtreeFiles = getSelectedRelDir()
        ? allFolderFiles.filter((f) => f.rel_path.startsWith(`${getSelectedRelDir()}/`))
        : allFolderFiles;
    const total = subtreeFiles.length;
    const indexed = subtreeFiles.filter((x) => x.state === "indexed").length;
    const errors = subtreeFiles.filter((x) => x.state === "error").length;
    const unsupported = subtreeFiles.filter((x) => x.state === "unsupported").length;
    // 'In progress' = chunks/images already committed (state ∈
    // {extracted, embedding}) but the file hasn't reached 'indexed' yet.
    // Pending = literally state == 'pending', i.e. not started. Splitting
    // these stops the sidebar from showing 'Pending: 329, Chunks: 1943'
    // when most of the work has actually landed and is just queued for
    // GPU embedding.
    const inProgress = subtreeFiles.filter(
        (x) => x.state === "extracted" || x.state === "embedding",
    ).length;
    const pending = subtreeFiles.filter((x) => x.state === "pending").length;
    $("#kv-files").textContent = total;
    $("#kv-indexed").textContent = indexed;
    $("#kv-errors").textContent = errors;
    $("#kv-pending").textContent = pending;
    const kvUnsupported = $("#kv-unsupported");
    if (kvUnsupported) kvUnsupported.textContent = unsupported;
    const kvInProgress = $("#kv-in-progress");
    if (kvInProgress) kvInProgress.textContent = inProgress;

    // Folder-level stats live in the ``folderStats`` store, fed by
    // ``folder.stats_changed`` over the WS. ``ensureFolderStats`` does
    // the first-load REST fetch on demand so the panel never shows "…"
    // for longer than the round-trip; subsequent updates flow over the
    // socket and the subscriber re-runs ``renderSidebar`` automatically.
    const s = folderStats.get().get(folder.id) || null;
    if (!s) ensureFolderStats(folder.id);
    $("#kv-bytes").textContent = s ? humanBytes(s.bytes_total) : "…";
    $("#kv-chunks").textContent = s ? s.chunks_total : "…";
    $("#kv-images").textContent = s ? s.images_total : "…";
    $("#kv-images-unique").textContent = s ? s.images_unique : "…";

    // Vector-store sanity badge: SQLite says these files are indexed but
    // Qdrant has 0 chunk points. Surfaced here (rather than as a search-time
    // surprise) because the user lives in this panel.
    const healthBadge = $("#folder-health-badge");
    if (s && s.index_health && s.index_health.status === "out_of_sync") {
        healthBadge.textContent = "⚠ Reindex needed";
        healthBadge.title =
            `${indexed} file(s) indexed in DB but ${s.index_health.qdrant_chunk_points} ` +
            `chunk points in vector store. Click Reindex to repopulate.`;
        healthBadge.hidden = false;
    } else {
        healthBadge.hidden = true;
    }

    // Live reindex pill — only present while the worker is in the wipe /
    // queue phase of a reindex_folder job for this folder. The backend
    // publishes folder.reindex_progress at ~5/s (one per 200-file chunk).
    // Once the job finishes, ws.js drops the entry and the badge hides.
    const reindexBadge = $("#folder-reindex-badge");
    const progress = reindexProgress.get().get(folder.id);
    if (progress) {
        // phase='queued' fires the moment the REST handler enqueues
        // the reindex job, before the worker actually picks it up.
        // While the worker is busy on another extract, this is what
        // the user sees — keep the message specific so the pill
        // doesn't look stuck.
        let label;
        if (progress.phase === "queued") {
            const behind = progress.detail?.behind;
            label = behind
                ? `↻ Queued behind ${behind}`
                : "↻ Queued";
        } else {
            const verb = progress.phase === "cancelling" ? "Cancelling stale jobs"
                : progress.phase === "wiping" ? "Wiping"
                : progress.phase === "queueing" ? "Queueing fresh extracts"
                : progress.phase;
            label = `↻ ${verb} — ${progress.done}/${progress.total}`;
        }
        reindexBadge.textContent = label;
        // Hide the "Reindex needed" warning while we're actively reindexing
        // so the two pills don't shout at each other.
        healthBadge.hidden = true;
        reindexBadge.hidden = false;
    } else {
        reindexBadge.hidden = true;
    }

    // Live sync pill — connector + worker emit folder.sync_progress as
    // the auth → list → download → clean phases run. Without this badge
    // the user sees "Status: none" for the entire initial sync (the
    // file-state-derived status pill can only count files that already
    // exist on disk, and the disk is empty until downloading lands).
    const syncBadge = $("#folder-sync-badge");
    const syncP = syncProgress.get().get(folder.id);
    if (syncP) {
        const phase = syncP.phase;
        const d = syncP.detail || {};
        let label;
        if (phase === "queued") {
            label = "↓ Queued";
        } else if (phase === "connecting") {
            label = "↓ Connecting to Drive";
        } else if (phase === "listing") {
            // Use the rich ``detail`` payload so the badge animates as
            // each Drive API page lands — without it the listing pill
            // sits motionless for tens of seconds on big folders. We
            // surface three things: which top-level folder we're on
            // (most useful single signal), running count of items seen,
            // and a side-of-the-bar items-skipped count when the ignore
            // matcher is dropping a lot of stuff.
            const parts = [];
            if (d.folders_total) {
                parts.push(`folder ${d.folders_done || 0}/${d.folders_total}`);
            }
            if (d.current_folder) {
                parts.push(`'${d.current_folder}'`);
            }
            if (typeof d.items_seen === "number") {
                parts.push(`${d.items_seen.toLocaleString()} items`);
            }
            if (d.items_skipped) {
                parts.push(`${d.items_skipped.toLocaleString()} skipped`);
            }
            label = parts.length
                ? `↓ Listing — ${parts.join(" · ")}`
                : "↓ Listing";
        } else if (phase === "fetching_docs") {
            // Parallel docs.get pass — one round-trip per Google Doc to
            // discover its tabs. Big speed win over the old serial path,
            // but still meaningful work; surface the counter so the user
            // sees progress while ~250 doc fetches run in parallel.
            label = syncP.total > 0
                ? `↓ Fetching docs — ${syncP.done}/${syncP.total}`
                : "↓ Fetching docs";
        } else if (phase === "downloading") {
            label = syncP.total > 0
                ? `↓ Downloading — ${syncP.done}/${syncP.total}`
                : "↓ Downloading";
        } else if (phase === "cleaning") {
            label = "↓ Cleaning up";
        } else {
            label = `↓ ${phase}`;
        }
        syncBadge.textContent = label;
        syncBadge.hidden = false;
    } else {
        syncBadge.hidden = true;
    }

    renderExtTable(s);

    $("#upload-target-hint").hidden = false;
    $("#upload-target").textContent = getSelectedRelDir() ? `/${getSelectedRelDir()}/` : "/";
}

// ----- Per-extension table -----
//
// Keyed reconciliation: keep one ``<tr>`` per extension alive across
// renders, only mutate the cells that changed. Same pattern as the
// folder tree and jobs list.
const extRowCache = new Map();

function buildExtRow(ext) {
    const tr = document.createElement("tr");
    tr.dataset.ext = ext;
    const tdExt = document.createElement("td");
    tdExt.className = "ext";
    tdExt.textContent = ext;
    const tdFiles = document.createElement("td");
    tdFiles.className = "num";
    const tdChunks = document.createElement("td");
    tdChunks.className = "num";
    tr.append(tdExt, tdFiles, tdChunks);
    tr._refs = { tdFiles, tdChunks };
    return tr;
}

function updateExtRow(tr, ext, e) {
    // Row class drives color coding:
    //   error      → any file under this ext failed
    //   unsupported → every file is unsupported (no parser)
    //   pending    → none indexed yet but work is moving
    //   indexed    → at least some chunks landed
    let rowClass = "";
    const tooltipBits = [];
    if (e.error > 0) {
        rowClass = "ext-error";
        tooltipBits.push(`${e.error} error`);
    } else if (e.indexed === 0 && e.unsupported === e.files) {
        rowClass = "ext-unsupported";
        tooltipBits.push(`${e.unsupported} unsupported (no parser)`);
    } else if (e.indexed === 0 && e.pending > 0) {
        rowClass = "ext-pending";
        tooltipBits.push(`${e.pending} pending`);
    } else if (e.indexed > 0) {
        rowClass = "ext-indexed";
    }
    if (e.indexed) tooltipBits.push(`${e.indexed} indexed`);
    if (e.unsupported && rowClass !== "ext-unsupported") tooltipBits.push(`${e.unsupported} unsupported`);
    if (e.pending && rowClass !== "ext-pending") tooltipBits.push(`${e.pending} pending`);
    setIfChanged(tr, "className", rowClass);
    const title = tooltipBits.join(" · ");
    if (tr.title !== title) tr.title = title;
    setIfChanged(tr._refs.tdFiles, "textContent", String(e.files));
    setIfChanged(tr._refs.tdChunks, "textContent", String(e.chunks));
}

function renderExtTable(s) {
    const extTable = $("#ext-table");
    const extTbody = $("#ext-tbody");
    // Sort by file count desc; falls back to ext name for stable order.
    const exts = s
        ? Object.entries(s.by_extension).sort(
            (a, b) => b[1].files - a[1].files || a[0].localeCompare(b[0]),
        )
        : [];
    extTable.hidden = exts.length === 0;

    const targetRows = [];
    const seenKeys = new Set();
    for (const [ext, e] of exts) {
        let tr = extRowCache.get(ext);
        if (!tr) {
            tr = buildExtRow(ext);
            extRowCache.set(ext, tr);
        }
        updateExtRow(tr, ext, e);
        targetRows.push(tr);
        seenKeys.add(ext);
    }

    reconcileChildren(extTbody, targetRows, seenKeys, extRowCache);
}

function humanBytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

// Tracks folder ids we've kicked a first-load fetch for so we don't
// fan out 60 identical REST calls when the rAF render loop fires
// before the first WS event lands.
const _statsInFlight = new Set();

export async function ensureFolderStats(folderId) {
    if (_statsInFlight.has(folderId)) return;
    if (folderStats.get().has(folderId)) return;
    _statsInFlight.add(folderId);
    try {
        const s = await api.folderStats(folderId);
        // The store's set-by-key is mutate-Map-then-update so the
        // subscriber sees a fresh Map identity and re-renders.
        folderStats.update((map) => {
            const next = new Map(map);
            next.set(folderId, s);
            return next;
        });
    } catch (err) {
        // 404 just means the folder was removed mid-render. Other
        // failures are logged once so the console doesn't drown if
        // the stats endpoint is down.
        console.warn("stats first-load failed", err);
    } finally {
        _statsInFlight.delete(folderId);
    }
}
