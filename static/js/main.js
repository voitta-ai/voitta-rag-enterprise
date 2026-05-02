// SPA entry point. Folder-list driven, with a selection-aware sidebar.

import { api } from "./api.js";
import { connStatus, files, folders, jobs, reindexProgress, syncProgress } from "./store.js";
import { connect } from "./ws.js";

const $ = (sel) => document.querySelector(sel);

let selectedFolderId = null;
let selectedRelDir = ""; // "" = folder root; otherwise "subdir/inner"
let rootInfo = { configured: false, root_path: null };
let statsCache = null; // last successful FolderStats response
let statsTimer = null;
const expandedNodes = new Set(); // keys: `${folder_id}:${rel_dir}`
const ghostDirs = new Map(); // folder_id → Set<rel_dir> (created via mkdir but no files yet)

function nodeKey(folderId, relDir) {
    return `${folderId}:${relDir}`;
}

// ----- Connection pill -----
connStatus.subscribe((s) => {
    const el = $("#conn-status");
    el.textContent = s;
    el.className = `status-pill ${s}`;
});

// ----- Stores -----
folders.subscribe((list) => {
    renderFolders(list);
    renderSidebar();
    updateToolbarState();
});
files.subscribe(() => {
    renderFolders(folders.get());
    renderSidebar();
    // The Sync / Config button visibility depends on whether the folder is
    // empty, which is computed from the files store — re-evaluate on change.
    updateToolbarState();
    scheduleStatsRefresh();
});
reindexProgress.subscribe(() => {
    // Progress events arrive at ~5/s during a wipe — re-render the
    // sidebar (which is the only place the badge lives) and the folder
    // list (so the row's status pill can flip if we ever surface it
    // there too). Cheap: both renders are O(folder count).
    renderSidebar();
    renderFolders(folders.get());
});
syncProgress.subscribe(() => {
    // Same cadence as reindex; identical re-render strategy.
    renderSidebar();
    renderFolders(folders.get());
});
jobs.subscribe(() => {
    renderJobs();
    // The tree's per-subtree status reads jobs.get() to decide between
    // "indexing" and "indexed" (see hasActiveWork in summariseSubtree). The
    // backend publishes file.upserted *before* the worker writes mark_done,
    // so when the last embed lands the file event arrives while the job is
    // still 'running' — and a moment later the job goes to 'done' but
    // nothing re-renders the tree. Re-render on jobs changes too so the
    // status flips to green without needing a manual expand/collapse.
    renderFolders(folders.get());
    // A job finishing usually means chunks/images counts moved.
    scheduleStatsRefresh();
});

function aggregateStatus(folderFiles) {
    if (folderFiles.length === 0) return "none";
    if (folderFiles.some((f) => f.state === "error")) return "error";
    if (folderFiles.every((f) => f.state === "indexed" || f.state === "unsupported")) return "indexed";
    return "indexing";
}

// Collapse the indexer's internal substate vocabulary into a small set of
// user-facing labels. ``unsupported`` is its own bucket so we can show it
// differently from a real failure — and from a still-in-progress file.
function userStateLabel(state) {
    if (state === "indexed" || state === "error" || state === "deleted" || state === "unsupported") return state;
    return "indexing";
}

// ---------- Tree model ----------

function buildTree(folderFiles, folderId) {
    /* Returns { dirs: Map<name, node>, files: [] } */
    const root = { dirs: new Map(), files: [] };
    for (const f of folderFiles) {
        if (f.state === "deleted") continue;
        const parts = f.rel_path.split("/").filter(Boolean);
        let node = root;
        for (let i = 0; i < parts.length - 1; i++) {
            const part = parts[i];
            if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
            node = node.dirs.get(part);
        }
        node.files.push(f);
    }
    // Merge in any ghost (mkdir-created) directories.
    const ghosts = ghostDirs.get(folderId);
    if (ghosts) {
        for (const relDir of ghosts) {
            const parts = relDir.split("/").filter(Boolean);
            let node = root;
            for (const part of parts) {
                if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
                node = node.dirs.get(part);
            }
        }
    }
    return root;
}

function activeFolderIds() {
    /* Map queued + running jobs back to folder ids so the per-row "indexing"
       pill only lights up on folders with work actually in flight.
       Previously the check was global ("any job running anywhere?") which
       made every folder containing a stale non-terminal file (left behind
       by a past abandoned job) flash to 'indexing' the moment another
       folder's reindex started — the bug the user filed.

       Job payload shapes (see services/job_queue.py + scanner / indexing):
       - extract / embed_text / delete_file: {file_id}
       - reindex_folder / sync:               {folder_id}
       embed_image runs inline within extract, never queued separately. */
    const fileFolder = new Map();
    for (const f of files.get()) fileFolder.set(f.id, f.folder_id);
    const out = new Set();
    for (const j of jobs.get()) {
        if (j.state !== "queued" && j.state !== "running") continue;
        const p = j.payload || {};
        if (p.folder_id != null) {
            out.add(p.folder_id);
        } else if (p.file_id != null) {
            const fid = fileFolder.get(p.file_id);
            if (fid != null) out.add(fid);
        }
    }
    return out;
}

function summariseSubtree(node, folderActive) {
    /* Aggregates file totals across the subtree rooted at node.

       ``folderActive`` is true when the queue currently has at least one
       job touching this subtree's folder. We require BOTH that signal AND
       a non-terminal file in the subtree to render 'indexing' — neither
       alone is sufficient (queue empty → stragglers; folder active but all
       this subtree's files are indexed → another subtree is the one moving). */
    let total = 0, indexed = 0, unsupported = 0, errored = 0, pending = 0, embedding = 0;
    function walk(n) {
        for (const f of n.files) {
            total++;
            if (f.state === "indexed") indexed++;
            else if (f.state === "unsupported") unsupported++;
            else if (f.state === "error") errored++;
            else if (f.state === "extracted" || f.state === "embedding" || f.pending_embeds > 0) embedding++;
            else pending++;
        }
        for (const child of n.dirs.values()) walk(child);
    }
    walk(node);
    let status = "none";
    if (total > 0) {
        if (errored > 0) status = "error";
        else if (indexed + unsupported === total) status = "indexed";
        else if (folderActive && (embedding > 0 || pending > 0)) status = "indexing";
        // No active jobs for this folder but some files aren't terminal —
        // they're stragglers, not work in flight. Reading 'indexing' here
        // is a lie; treat the subtree as done so the UI matches the queue.
        else status = "indexed";
    }
    return { total, indexed, unsupported, errored, status };
}

// ---------- Tree rendering ----------

// Build a small iOS-style toggle switch. ``onChange(nextChecked)`` runs in
// response to the underlying input firing — we stop propagation so clicking
// the switch doesn't also select the folder row.
function buildSwitch({ checked, disabled, title, onChange }) {
    const wrap = document.createElement("label");
    wrap.className = "folder-switch";
    wrap.title = title || "";
    wrap.addEventListener("click", (e) => e.stopPropagation());

    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!checked;
    input.disabled = !!disabled;
    input.addEventListener("change", () => onChange(input.checked));

    const track = document.createElement("span");
    track.className = "track";

    wrap.append(input, track);
    return wrap;
}

async function toggleFolderActive(folder, active) {
    try {
        const updated = await api.setFolderActive(folder.id, active);
        const next = folders.get().map((f) => (f.id === folder.id ? updated : f));
        folders.set(next);
    } catch (err) {
        alert(err.message);
        renderFolders(folders.get()); // restore the original visual state
    }
}

async function toggleFolderShare(folder, shared) {
    try {
        const updated = await api.setFolderShare(folder.id, shared);
        const next = folders.get().map((f) => (f.id === folder.id ? updated : f));
        folders.set(next);
    } catch (err) {
        alert(err.message);
        renderFolders(folders.get());
    }
}

function renderFolders(list) {
    const ul = $("#folder-list");
    ul.innerHTML = "";
    const sorted = [...list].sort((a, b) => a.id - b.id);
    if (sorted.length === 0) {
        const empty = document.createElement("li");
        empty.className = "tree-row";
        empty.style.gridTemplateColumns = "1fr";
        empty.style.color = "var(--color-text-secondary)";
        empty.textContent = "No folders yet — create or add one above.";
        ul.append(empty);
        return;
    }
    const allFiles = files.get();
    // Compute the active-work set once per render — it's identical for
    // every row so per-subtree recomputation would be wasted work.
    const activeFolders = activeFolderIds();
    for (const folder of sorted) {
        const folderFiles = allFiles.filter((x) => x.folder_id === folder.id);
        const tree = buildTree(folderFiles, folder.id);
        renderTreeRow({
            ul,
            folder,
            node: tree,
            relDir: "",
            displayName: folder.display_name,
            depth: 0,
            isRoot: true,
            folderActive: activeFolders.has(folder.id),
        });
    }
}

function renderTreeRow({ ul, folder, node, relDir, displayName, depth, isRoot, folderActive }) {
    const summary = summariseSubtree(node, !!folderActive);
    const key = nodeKey(folder.id, relDir);
    const hasChildren = node.dirs.size > 0 || node.files.length > 0;
    const isOpen = expandedNodes.has(key);
    const isSelected = folder.id === selectedFolderId && relDir === selectedRelDir;
    const canHaveChildren = isRoot || true; // dir nodes always

    // Shared-readonly = someone else's shared folder. We render it with an
    // accent strip so the user doesn't confuse it with their own folders.
    const sharedReadonly = isRoot && folder.shared && !folder.owned;

    const li = document.createElement("li");
    li.className =
        `tree-row ${isRoot ? "folder-root" : "dir"}` +
        (isSelected ? " selected" : "") +
        (sharedReadonly ? " shared-readonly" : "");
    li.dataset.key = key;

    // chevron + glyph + label live in one grid cell so they indent as a
    // single unit. Depth-based padding goes on the cell, not on chevron
    // or label — that way the metadata columns to the right stay aligned
    // with the column header regardless of nesting depth.
    const nameCell = document.createElement("span");
    nameCell.className = "name-cell";
    if (depth > 0) nameCell.style.paddingLeft = `${depth * 14}px`;

    const chevron = document.createElement("span");
    chevron.className = "chevron" + (isOpen ? " open" : "") + (hasChildren ? "" : " leaf");
    chevron.textContent = "▸";
    chevron.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!hasChildren) return;
        if (isOpen) expandedNodes.delete(key); else expandedNodes.add(key);
        renderFolders(folders.get());
    });
    nameCell.append(chevron);

    const label = document.createElement("span");
    label.className = "label";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    glyph.textContent = isRoot ? "▣" : "📁";
    const text = document.createElement("span");
    text.textContent = displayName;
    label.append(glyph, text);
    nameCell.append(label);
    li.append(nameCell);

    const fileCount = document.createElement("span");
    fileCount.className = "num";
    fileCount.textContent = summary.total || "";
    li.append(fileCount);

    const indexedCount = document.createElement("span");
    indexedCount.className = "num";
    indexedCount.textContent = summary.total
        ? `${summary.indexed}${summary.errored ? ` · ${summary.errored}!` : ""}`
        : "";
    li.append(indexedCount);

    const tag = document.createElement("span");
    tag.className = `status-tag ${summary.status}`;
    tag.textContent = summary.status;
    li.append(tag);

    if (isRoot) {
        // Per-user MCP-search toggle. Available on every visible folder
        // (including shared ones the user doesn't own — it's their personal
        // preference, not a folder-level mutation).
        li.append(
            buildSwitch({
                title: folder.active
                    ? "MCP search includes this folder. Click to exclude."
                    : "MCP search excludes this folder. Click to include.",
                checked: folder.active,
                disabled: false,
                onChange: (next) => toggleFolderActive(folder, next),
            }),
        );
        // Owner-only Share toggle. We still render the cell (so the grid
        // stays aligned) but with a hidden / disabled switch for non-owners.
        if (folder.owned) {
            li.append(
                buildSwitch({
                    title: folder.shared
                        ? "Folder is shared with everyone. Click to unshare."
                        : "Folder is private. Click to share with everyone.",
                    checked: folder.shared,
                    disabled: false,
                    onChange: (next) => toggleFolderShare(folder, next),
                    iconBefore: "↗",
                }),
            );
        } else {
            // Filler so the grid columns line up across rows.
            const spacer = document.createElement("span");
            spacer.className = "folder-switch";
            spacer.style.visibility = "hidden";
            li.append(spacer);
        }
    } else {
        // Non-root rows still have to fill the grid; otherwise the row
        // collapses to fewer columns and looks misaligned next to roots.
        for (let i = 0; i < 2; i++) {
            const spacer = document.createElement("span");
            spacer.style.visibility = "hidden";
            li.append(spacer);
        }
    }


    li.addEventListener("click", () => selectNode(folder.id, relDir));
    ul.append(li);

    if (isOpen) {
        // Subdirs first.
        for (const [name, child] of [...node.dirs.entries()].sort()) {
            const childRelDir = relDir ? `${relDir}/${name}` : name;
            renderTreeRow({
                ul,
                folder,
                node: child,
                relDir: childRelDir,
                displayName: name,
                depth: depth + 1,
                isRoot: false,
                folderActive,
            });
        }
        // Then files.
        for (const f of [...node.files].sort((a, b) => a.rel_path.localeCompare(b.rel_path))) {
            renderFileRow(ul, folder, f, depth + 1);
        }
    }
}

function renderFileRow(ul, folder, file, depth) {
    const li = document.createElement("li");
    li.className = "tree-row file";
    li.dataset.fileId = file.id;

    const nameCell = document.createElement("span");
    nameCell.className = "name-cell";
    if (depth > 0) nameCell.style.paddingLeft = `${depth * 14}px`;

    const chevron = document.createElement("span");
    chevron.className = "chevron leaf";
    chevron.textContent = "·";
    nameCell.append(chevron);

    const label = document.createElement("span");
    label.className = "label";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    glyph.textContent = "·";
    const text = document.createElement("span");
    const basename = file.rel_path.split("/").pop();
    text.textContent = basename;
    label.append(glyph, text);
    label.title = file.rel_path;
    nameCell.append(label);
    li.append(nameCell);

    const blank1 = document.createElement("span");
    const blank2 = document.createElement("span");
    li.append(blank1, blank2);

    const tag = document.createElement("span");
    const stateLabel = userStateLabel(file.state);
    tag.className = `status-tag ${stateLabel}`;
    tag.textContent = stateLabel;
    tag.title = `state=${file.state}, pending_embeds=${file.pending_embeds}`;
    li.append(tag);

    ul.append(li);
}

function selectNode(folderId, relDir) {
    selectedFolderId = folderId;
    selectedRelDir = relDir;
    statsCache = null;
    renderFolders(folders.get());
    renderSidebar();
    refreshStats();
    updateToolbarState();
}

function updateToolbarState() {
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    const isManaged = !!(folder && folder.managed);
    const isRoot = !!folder && selectedRelDir === "";
    // Read-only = a shared folder owned by someone else. Owner-only mutations
    // (upload, mkdir, reindex, sync, remove) are disabled; viewers can still
    // expand the tree and read files.
    const isOwned = !!(folder && folder.owned);
    const readOnly = !!folder && !isOwned;

    $("#btn-new-subfolder").disabled = !isManaged || readOnly;
    $("#btn-upload").disabled = !isManaged || readOnly;
    $("#btn-reindex").disabled = !folder || readOnly;
    // Sync button: only at the folder root on managed folders. Hidden when
    // the folder is non-empty AND has no sync source — sync can't be
    // configured on an existing folder of files. When a sync source already
    // exists, the same button reads "Config" (re-opens the same modal).
    const syncBtn = $("#btn-sync");
    if (!(isManaged && isRoot) || readOnly) {
        syncBtn.hidden = true;
        syncBtn.disabled = true;
    } else {
        const hasSync = !!folder.has_sync_source;
        const folderFiles = files.get().filter(
            (x) => x.folder_id === folder.id && x.state !== "deleted",
        );
        const isEmpty = folderFiles.length === 0;
        if (!hasSync && !isEmpty) {
            syncBtn.hidden = true;
            syncBtn.disabled = true;
        } else {
            // The HTML starts with `disabled` so the button doesn't flicker
            // before the first updateToolbarState — clear it whenever the
            // button is meant to be usable.
            syncBtn.hidden = false;
            syncBtn.disabled = false;
            syncBtn.textContent = hasSync ? "🔄 Config" : "🔄 Sync";
            syncBtn.title = hasSync
                ? "Edit the remote sync configuration for this folder"
                : "Configure a remote sync (e.g. GitHub) for this empty folder";
        }
    }
    $("#btn-remove").disabled = !isRoot || readOnly;
}

async function createSubfolder() {
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    if (!folder?.managed) return;
    const name = prompt("Subfolder name:");
    if (!name?.trim()) return;
    const target = selectedRelDir
        ? `${selectedRelDir}/${name.trim()}`
        : name.trim();
    try {
        await api.mkdir(folder.id, target);
        if (!ghostDirs.has(folder.id)) ghostDirs.set(folder.id, new Set());
        ghostDirs.get(folder.id).add(target);
        expandedNodes.add(nodeKey(folder.id, selectedRelDir));
        renderFolders(folders.get());
    } catch (err) {
        alert(err.message);
    }
}

// ----- Sidebar -----

function renderSidebar() {
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    const empty = $("#sidebar-empty");
    const detail = $("#folder-detail");

    if (!folder) {
        empty.hidden = false;
        detail.hidden = true;
        return;
    }

    empty.hidden = true;
    detail.hidden = false;

    const displayName = selectedRelDir
        ? `${folder.display_name}/${selectedRelDir}`
        : folder.display_name;
    $("#folder-name").textContent = displayName;
    $("#folder-path").textContent = selectedRelDir
        ? `${folder.path}/${selectedRelDir}`
        : folder.path;
    $("#folder-managed-badge").hidden = !folder.managed;
    $("#folder-source-badge").textContent = folder.source_type;

    // Subtree-scoped counts (fall back to whole folder when relDir is empty).
    const allFolderFiles = files.get().filter((x) => x.folder_id === folder.id && x.state !== "deleted");
    const subtreeFiles = selectedRelDir
        ? allFolderFiles.filter((f) => f.rel_path.startsWith(`${selectedRelDir}/`))
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

    // Folder-level stats from /api/folders/{id}/stats — independent of subdir.
    const s = statsCache && statsCache.folder_id === folder.id ? statsCache : null;
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
        const verb = progress.phase === "cancelling" ? "Cancelling stale jobs"
            : progress.phase === "wiping" ? "Wiping"
            : progress.phase === "queueing" ? "Queueing fresh extracts"
            : progress.phase;
        reindexBadge.textContent = `↻ ${verb} — ${progress.done}/${progress.total}`;
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
        let label;
        if (phase === "queued") {
            label = "↓ Queued";
        } else if (phase === "connecting") {
            label = "↓ Connecting to Drive";
        } else if (phase === "listing") {
            // total here = number of picked Drive folders being enumerated.
            label = syncP.total > 0
                ? `↓ Listing — ${syncP.done}/${syncP.total} folders`
                : "↓ Listing";
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

    const extTable = $("#ext-table");
    const extTbody = $("#ext-tbody");
    extTbody.innerHTML = "";
    // Sort by file count desc; falls back to ext name for stable order.
    const exts = s
        ? Object.entries(s.by_extension).sort((a, b) => b[1].files - a[1].files || a[0].localeCompare(b[0]))
        : [];
    extTable.hidden = exts.length === 0;
    for (const [ext, e] of exts) {
        const tr = document.createElement("tr");
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
        tr.className = rowClass;
        tr.title = tooltipBits.join(" · ");
        const tdExt = document.createElement("td");
        tdExt.className = "ext";
        tdExt.textContent = ext;
        const tdFiles = document.createElement("td");
        tdFiles.className = "num";
        tdFiles.textContent = e.files;
        const tdChunks = document.createElement("td");
        tdChunks.className = "num";
        tdChunks.textContent = e.chunks;
        tr.append(tdExt, tdFiles, tdChunks);
        extTbody.append(tr);
    }

    const hint = $("#upload-target-hint");
    hint.hidden = !folder.managed;
    $("#upload-target").textContent = selectedRelDir ? `/${selectedRelDir}/` : "/";
}

function humanBytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

async function refreshStats() {
    if (!selectedFolderId) return;
    const id = selectedFolderId;
    try {
        const s = await api.folderStats(id);
        if (id === selectedFolderId) {
            statsCache = s;
            renderSidebar();
        }
    } catch (err) {
        // Folder might have been deleted; surface only the first error.
        console.warn("stats fetch failed", err);
    }
}

function scheduleStatsRefresh() {
    if (!selectedFolderId) return;
    if (statsTimer) clearTimeout(statsTimer);
    statsTimer = setTimeout(() => { statsTimer = null; refreshStats(); }, 400);
}

// ----- Jobs -----

function renderJobs() {
    const ul = $("#jobs");
    ul.innerHTML = "";
    for (const j of jobs.get().slice(0, 30)) {
        const li = document.createElement("li");
        const col = document.createElement("div");
        col.className = "col";
        const top = document.createElement("span");
        top.textContent = `${j.kind} #${j.id}`;
        col.append(top);
        if (j.state === "error" && j.error) {
            const err = document.createElement("span");
            err.className = "err";
            err.textContent = j.error.length > 200 ? j.error.slice(0, 200) + "…" : j.error;
            col.append(err);
        }
        li.append(col);

        const tag = document.createElement("span");
        tag.className = `status-tag ${j.state}`;
        tag.textContent = j.state;
        li.append(tag);

        if (j.state === "error") {
            const retry = document.createElement("button");
            retry.className = "retry";
            retry.textContent = "↻";
            retry.title = "Retry";
            retry.addEventListener("click", async () => {
                try { await api.retryJob(j.id); } catch (err) { alert(err.message); }
            });
            li.append(retry);
        }
        ul.append(li);
    }
}

// ----- Toolbar / sidebar actions -----

$("#btn-new-folder").addEventListener("click", () => openModal("managed"));
$("#btn-add-existing").addEventListener("click", () => openModal("picker"));
$("#modal-close").addEventListener("click", closeModal);
$("#modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
});

$("#btn-new-subfolder").addEventListener("click", createSubfolder);

$("#btn-upload").addEventListener("click", () => $("#upload-input").click());
$("#upload-input").addEventListener("change", async (e) => {
    const selected = Array.from(e.target.files);
    if (!selected.length || !selectedFolderId) return;

    const wrap = $("#upload-progress");
    const fill = $("#upload-progress-fill");
    const label = $("#upload-progress-label");
    const totalBytes = selected.reduce((sum, f) => sum + (f.size || 0), 0);
    wrap.hidden = false;
    fill.style.width = "0%";
    label.textContent = `Uploading ${selected.length} file(s)…`;

    try {
        await api.upload(
            selectedFolderId,
            selected,
            selectedRelDir,
            ({ loaded, total, fraction }) => {
                const pct = Math.round(fraction * 100);
                fill.style.width = `${pct}%`;
                if (total) {
                    label.textContent =
                        `Uploading ${selected.length} file(s) — ${pct}%`;
                }
            },
        );
        // Bytes are off the client; the server still has to write them and
        // the watcher will fire indexing. Show a brief "saving" state so it
        // doesn't look like the bar got stuck at 100%.
        fill.style.width = "100%";
        label.textContent = "Saving…";
        // Give the watcher events ~600ms to flow back, then hide.
        setTimeout(() => { wrap.hidden = true; }, 600);
    } catch (err) {
        wrap.hidden = true;
        alert(err.message);
    } finally {
        e.target.value = "";
        // discount unused vars
        void totalBytes;
    }
});

$("#btn-reindex").addEventListener("click", async () => {
    if (!selectedFolderId) return;
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    if (!folder) return;
    const allFolderFiles = files.get().filter(
        (x) => x.folder_id === folder.id && x.state !== "deleted",
    );
    const subtreeFiles = selectedRelDir
        ? allFolderFiles.filter((f) => f.rel_path.startsWith(`${selectedRelDir}/`))
        : allFolderFiles;
    if (subtreeFiles.length === 0) {
        alert("No files to reindex in this subtree.");
        return;
    }
    const where = selectedRelDir
        ? `${folder.display_name}/${selectedRelDir}`
        : folder.display_name;
    const ok = confirm(
        `Hard re-index ${subtreeFiles.length} file(s) under "${where}"?\n\n` +
        `Every file in this subtree will be re-parsed and re-embedded ` +
        `from scratch — this can take a while and will keep workers busy.\n\n` +
        `Existing chunks and image embeddings remain available until the ` +
        `new ones are committed.`,
    );
    if (!ok) return;
    try {
        const r = await api.reindexFolder(selectedFolderId, selectedRelDir);
        if (r.scheduled === 0) alert("No files were scheduled.");
    } catch (err) {
        alert(err.message);
    }
});

$("#btn-remove").addEventListener("click", async () => {
    if (!selectedFolderId) return;
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    if (!folder) return;
    if (!confirm(`Unregister "${folder.display_name}"?\n\nFiles on disk are not deleted.`)) return;
    try {
        await api.deleteFolder(selectedFolderId);
        selectedFolderId = null;
        selectedRelDir = "";
    } catch (err) {
        alert(err.message);
    }
});

$("#btn-retry-all").addEventListener("click", async () => {
    try {
        const r = await api.retryAllFailed();
        if (r.retried === 0) alert("No failed jobs to retry");
    } catch (err) { alert(err.message); }
});
$("#btn-clear-failed").addEventListener("click", async () => {
    if (!confirm("Permanently delete all failed-job records?")) return;
    try {
        await api.cleanupFailedJobs();
        jobs.set(await api.recentJobs());
    } catch (err) { alert(err.message); }
});

// ----- Modal: create / picker -----

function openModal(mode) {
    $("#modal-backdrop").hidden = false;
    if (mode === "managed") {
        if (!rootInfo.configured) {
            alert("Set VOITTA_ROOT_PATH in .env to create new folders.");
            $("#modal-backdrop").hidden = true;
            return;
        }
        $("#modal-title").textContent = "New folder";
        $("#modal-managed").hidden = false;
        $("#modal-picker").hidden = true;
        $("#modal-root").textContent = rootInfo.root_path;
        $("#managed-name").value = "";
        $("#managed-name").focus();
    } else {
        $("#modal-title").textContent = "Add existing folder";
        $("#modal-managed").hidden = true;
        $("#modal-picker").hidden = false;
        pickerNavigate(null);
    }
}

function closeModal() {
    $("#modal-backdrop").hidden = true;
}

$("#managed-create").addEventListener("click", async () => {
    const name = $("#managed-name").value.trim();
    if (!name) return;
    try {
        await api.addFolderByName(name);
        closeModal();
    } catch (err) { alert(err.message); }
});

let pickerCwd = null;
async function pickerNavigate(path) {
    try {
        const res = await api.fsList(path);
        pickerCwd = res.path;
        $("#picker-path").textContent = res.path;
        const ul = $("#picker-list");
        ul.innerHTML = "";
        for (const entry of res.entries) {
            const li = document.createElement("li");
            li.textContent = entry.name;
            li.className = entry.is_dir ? "dir" : "file";
            if (entry.is_dir) {
                li.addEventListener("click", () => {
                    const sep = res.path === "/" ? "" : "/";
                    pickerNavigate(`${res.path}${sep}${entry.name}`);
                });
            }
            ul.append(li);
        }
        $("#picker-up").disabled = !res.parent;
        $("#picker-up").dataset.target = res.parent || "";
    } catch (err) { alert(err.message); }
}

$("#picker-up").addEventListener("click", () => {
    const target = $("#picker-up").dataset.target;
    if (target) pickerNavigate(target);
});
$("#picker-pick").addEventListener("click", async () => {
    if (!pickerCwd) return;
    try {
        await api.addFolderByPath(pickerCwd);
        closeModal();
    } catch (err) { alert(err.message); }
});

// ----- Sync modal -----
//
// Shape: every managed folder root has zero or one sync source. The modal
// loads the existing config (if any), lets the user edit + save, and offers
// "Sync now" to enqueue a sync job. Branch selection requires hitting the
// remote (POST /sync/branches) — the user fills in repo + auth, clicks
// "Load branches", then picks from the dropdown. Credentials in fields stay
// in the form until Save (they aren't echoed back from the server — only
// has_pat / has_ssh_key flags are returned, used to gray out the inputs).

let syncFolderId = null;

function openSyncModal() {
    if (!selectedFolderId) return;
    syncFolderId = selectedFolderId;
    const folder = folders.get().find((f) => f.id === syncFolderId);
    if (!folder) return;

    $("#sync-title").textContent = `Configure sync — ${folder.display_name}`;
    $("#sync-backdrop").hidden = false;
    $("#sync-status-line").hidden = true;
    $("#sync-delete").hidden = true;

    // Reset to defaults; loadSyncSource() will fill from server if present.
    $("#sync-type").value = "github";
    setSyncType("github");
    $("#sync-gh-repo").value = "";
    $("#sync-gh-path").value = "";
    setGhAuth("ssh");
    $("#sync-gh-ssh-key").value = "";
    $("#sync-gh-username").value = "";
    $("#sync-gh-pat").value = "";
    $("#sync-gh-all-branches").checked = false;
    $("#sync-gh-branches").innerHTML = "";
    $("#sync-gh-extended").checked = false;
    $("#sync-gd-client-id").value = "";
    $("#sync-gd-client-secret").value = "";
    $("#sync-gd-client-secret").placeholder = "GOCSPX-…";
    $("#sync-gd-add-folder-id").value = "";
    setGdFolders([]);
    $("#sync-gd-sa-json").value = "";
    $("#sync-gd-sa-json").placeholder = '{"type":"service_account","client_email":"…","private_key":"…"}';
    setGdAuthMode("oauth");
    setGdConnState({ connected: false, hasClientSecret: false });

    loadSyncSource();
}

function setSyncType(t) {
    $("#sync-form-github").hidden = t !== "github";
    $("#sync-form-google_drive").hidden = t !== "google_drive";
    if (t === "google_drive") {
        // Mirror the URL the backend will hand to Google so the user can
        // copy-paste it verbatim into "Authorized redirect URIs".
        const hint = $("#sync-gd-redirect-hint");
        if (hint) hint.textContent = `${window.location.origin}/api/sync/oauth/google/callback`;
    }
}

// Mirror of the user's current selection in the GD picker; flushed to the
// form on every set. Keeps the rendered list and the form-config call in
// sync without parsing the DOM at submit time.
let gdFolders = [];

// Which credential pane is showing — drives both the rendered inputs and
// what gets serialised in gdFormConfig. The two flows have meaningfully
// different gotchas (OAuth needs Connect + redirect URI; SA needs
// folder-shared-with-client_email), so we render exactly one set of
// fields and persist the mode the user actually used.
let gdAuthMode = "oauth"; // "oauth" | "sa"

function setGdAuthMode(mode) {
    gdAuthMode = mode === "sa" ? "sa" : "oauth";
    const oauthTab = $("#sync-gd-tab-oauth");
    const saTab = $("#sync-gd-tab-sa");
    oauthTab.classList.toggle("active", gdAuthMode === "oauth");
    oauthTab.setAttribute("aria-selected", gdAuthMode === "oauth" ? "true" : "false");
    saTab.classList.toggle("active", gdAuthMode === "sa");
    saTab.setAttribute("aria-selected", gdAuthMode === "sa" ? "true" : "false");
    $("#sync-gd-pane-oauth").hidden = gdAuthMode !== "oauth";
    $("#sync-gd-pane-sa").hidden = gdAuthMode !== "sa";

    // Pick browser only works in OAuth mode (it needs gd_refresh_token).
    // In SA mode, surface the typed-folder-id input as the only path and
    // keep the Pick button visible-but-disabled with a clear hint.
    const pickBtn = $("#sync-gd-pick-folder");
    const idInput = $("#sync-gd-add-folder-id");
    if (gdAuthMode === "sa") {
        // Pick works in SA mode too — the backend mints an SA access token
        // and lists everything shared with the SA's client_email plus any
        // Shared Drives it's a member of. We still need a saved SA JSON
        // before the API call will succeed.
        const hasSaSaved = $("#sync-gd-sa-json").placeholder.startsWith(
            "(service account JSON saved",
        );
        const hasSaTyped = $("#sync-gd-sa-json").value.trim().length > 0;
        const saReady = hasSaSaved || hasSaTyped;
        pickBtn.hidden = false;
        pickBtn.disabled = !saReady;
        pickBtn.title = saReady
            ? "Pick a folder shared with the service account"
            : "Save a service-account JSON first";
        idInput.placeholder = "Or paste a folder ID and press Enter";
        $("#sync-gd-folders-hint").innerHTML =
            "Pick from folders shared with the service account, or paste a Drive " +
            "folder ID directly. <strong>The service account only sees folders explicitly " +
            "shared with its <code>client_email</code></strong> (Viewer is enough). Each " +
            "folder syncs into its own subdirectory under this folder.";
    } else {
        pickBtn.hidden = false;
        idInput.placeholder = "Or paste a folder ID and press Enter";
        // The OAuth pane controls Pick-button enablement via setGdConnState
        // — call it with whatever connection state we currently know.
        const connected = $("#sync-gd-conn-status").textContent.startsWith("Connected");
        const hasClientSecret = $("#sync-gd-client-secret").placeholder.startsWith("(saved");
        setGdConnState({ connected, hasClientSecret });
        $("#sync-gd-folders-hint").textContent =
            "Each picked folder syncs into its own subdirectory under this folder.";
    }
}

function setGdFolders(folders) {
    gdFolders = (folders || []).map((f) => ({ id: String(f.id || ""), name: String(f.name || "") }));
    const ul = $("#sync-gd-folders-list");
    ul.innerHTML = "";
    if (gdFolders.length === 0) {
        $("#sync-gd-folder-count").textContent = "none selected";
    } else {
        $("#sync-gd-folder-count").textContent = `${gdFolders.length} folder${gdFolders.length === 1 ? "" : "s"} selected`;
        for (const f of gdFolders) {
            const li = document.createElement("li");
            li.textContent = f.name ? `${f.name}` : f.id;
            ul.append(li);
        }
    }
}

function setGdConnState({ connected, hasClientSecret }) {
    $("#sync-gd-conn-status").textContent = connected ? "Connected ✓" : "Not connected";
    const connectBtn = $("#sync-gd-connect");
    const pickBtn = $("#sync-gd-pick-folder");
    // Connect needs client_id + (saved or just-typed) client_secret.
    const clientId = $("#sync-gd-client-id").value.trim();
    const secretAvailable = hasClientSecret || $("#sync-gd-client-secret").value.trim().length > 0;
    connectBtn.disabled = !(clientId && secretAvailable);
    connectBtn.title = connectBtn.disabled
        ? "Save client_id and client_secret first"
        : (connected ? "Re-connect (forces a fresh consent)" : "Connect");
    // Pick is OAuth-only — leave the SA-mode disabled state alone.
    if (gdAuthMode === "oauth") {
        pickBtn.disabled = !connected;
        pickBtn.title = connected ? "Pick a Drive folder" : "Connect first";
    }
}

$("#sync-gd-tab-oauth").addEventListener("click", () => setGdAuthMode("oauth"));
$("#sync-gd-tab-sa").addEventListener("click", () => setGdAuthMode("sa"));

// Manual folder-ID add — Enter accepts, ignores duplicates and empties.
// Folder names aren't known here (no Drive lookup in SA mode); the
// rendered list falls back to showing the bare ID, which is fine.
$("#sync-gd-add-folder-id").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const id = $("#sync-gd-add-folder-id").value.trim();
    if (!id) return;
    if (gdFolders.some((f) => f.id === id)) {
        $("#sync-gd-add-folder-id").value = "";
        return;
    }
    setGdFolders([...gdFolders, { id, name: "" }]);
    $("#sync-gd-add-folder-id").value = "";
});

function closeSyncModal() {
    $("#sync-backdrop").hidden = true;
    syncFolderId = null;
}

function setGhAuth(method) {
    $("#sync-gh-ssh").hidden = method !== "ssh";
    $("#sync-gh-token").hidden = method !== "token";
    document.querySelectorAll('input[name="sync-gh-auth"]').forEach((el) => {
        el.checked = el.value === method;
    });
}

async function loadSyncSource() {
    try {
        const src = await api.getSync(syncFolderId);
        if (!src) return;
        $("#sync-type").value = src.source_type;
        setSyncType(src.source_type);
        if (src.source_type === "github" && src.github) {
            const gh = src.github;
            $("#sync-gh-repo").value = gh.repo || "";
            $("#sync-gh-path").value = gh.path || "";
            setGhAuth(gh.auth_method || "ssh");
            $("#sync-gh-username").value = gh.username || "";
            $("#sync-gh-pat").placeholder = gh.has_pat ? "(token saved — type to replace)" : "ghp_…";
            $("#sync-gh-ssh-key").placeholder = gh.has_ssh_key ? "(SSH key saved — paste a new one to replace)" : "-----BEGIN OPENSSH PRIVATE KEY-----…";
            $("#sync-gh-all-branches").checked = !!gh.all_branches;
            $("#sync-gh-extended").checked = !!gh.extended;
            // Pre-populate branches dropdown with the saved selection so the
            // user sees what's currently configured even before hitting "Load".
            const sel = $("#sync-gh-branches");
            sel.innerHTML = "";
            for (const b of gh.branches || []) {
                const opt = document.createElement("option");
                opt.value = b;
                opt.textContent = b;
                opt.selected = true;
                sel.append(opt);
            }
        } else if (src.source_type === "google_drive" && src.google_drive) {
            const gd = src.google_drive;
            $("#sync-gd-client-id").value = gd.client_id || "";
            setGdFolders(gd.folders || []);
            $("#sync-gd-client-secret").placeholder = gd.has_client_secret ? "(saved — type to replace)" : "GOCSPX-…";
            $("#sync-gd-sa-json").placeholder = gd.has_service_account ? "(service account JSON saved — paste a new one to replace)" : '{"type":"service_account","client_email":"…","private_key":"…"}';
            // Pick the right tab. If the saved config has a service-account
            // key (and only that), surface SA mode; otherwise default to
            // OAuth — that's the more common path and the one the redirect-
            // URI hint is most useful for. Setting the mode AFTER
            // populating the inputs so setGdConnState reads the right
            // placeholders.
            const saOnly = gd.has_service_account && !gd.has_client_secret;
            setGdAuthMode(saOnly ? "sa" : "oauth");
            setGdConnState({ connected: gd.connected, hasClientSecret: gd.has_client_secret });
        }
        $("#sync-delete").hidden = false;
        renderSyncStatus(src);
    } catch (err) {
        // 404 just means no source yet — no UI feedback needed.
        if (!String(err.message || "").startsWith("404")) {
            console.warn("getSync failed", err);
        }
    }
}

function renderSyncStatus(src) {
    const line = $("#sync-status-line");
    if (!src) { line.hidden = true; return; }
    const parts = [`status: ${src.sync_status}`];
    if (src.last_synced_at) {
        const d = new Date(src.last_synced_at * 1000);
        parts.push(`last: ${d.toLocaleString()}`);
    }
    if (src.sync_error) parts.push(`error: ${src.sync_error}`);
    line.textContent = parts.join(" · ");
    line.hidden = false;
}

function ghFormConfig() {
    const authMethod = document.querySelector('input[name="sync-gh-auth"]:checked')?.value || "ssh";
    const sel = $("#sync-gh-branches");
    const branches = [...sel.selectedOptions].map((o) => o.value);
    return {
        repo: $("#sync-gh-repo").value.trim(),
        path: $("#sync-gh-path").value.trim(),
        auth_method: authMethod,
        username: $("#sync-gh-username").value.trim(),
        pat: $("#sync-gh-pat").value,           // not trimmed — preserve
        ssh_key: $("#sync-gh-ssh-key").value,   // not trimmed — preserve
        branches,
        all_branches: $("#sync-gh-all-branches").checked,
        extended: $("#sync-gh-extended").checked,
    };
}

function gdFormConfig() {
    // Only send credentials for the active mode. The inactive pane's
    // inputs may still hold leftover text the user typed before switching
    // tabs — sending both would let the back end pick whichever path it
    // prefers, which would be confusing. Empty strings are safe: the
    // back-end PUT validator preserves stored secrets when blank values
    // arrive (so "saved" placeholders aren't wiped on every save).
    if (gdAuthMode === "sa") {
        return {
            client_id: "",
            client_secret: "",
            folders: gdFolders,
            service_account_json: $("#sync-gd-sa-json").value,
        };
    }
    return {
        client_id: $("#sync-gd-client-id").value.trim(),
        client_secret: $("#sync-gd-client-secret").value,
        folders: gdFolders,
        service_account_json: "",
    };
}

function syncBody() {
    const t = $("#sync-type").value;
    if (t === "github") return { source_type: "github", github: ghFormConfig() };
    if (t === "google_drive") return { source_type: "google_drive", google_drive: gdFormConfig() };
    throw new Error(`Unknown source_type: ${t}`);
}

$("#sync-type").addEventListener("change", () => setSyncType($("#sync-type").value));

document.querySelectorAll('input[name="sync-gh-auth"]').forEach((el) => {
    el.addEventListener("change", () => setGhAuth(el.value));
});

// Re-evaluate Connect-button state when the user types in the GD inputs.
$("#sync-gd-client-id").addEventListener("input", () =>
    setGdConnState({
        connected: $("#sync-gd-conn-status").textContent.startsWith("Connected"),
        hasClientSecret: $("#sync-gd-client-secret").placeholder.startsWith("(saved"),
    }));
$("#sync-gd-client-secret").addEventListener("input", () =>
    setGdConnState({
        connected: $("#sync-gd-conn-status").textContent.startsWith("Connected"),
        hasClientSecret: $("#sync-gd-client-secret").placeholder.startsWith("(saved"),
    }));

$("#sync-close").addEventListener("click", closeSyncModal);
$("#sync-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "sync-backdrop") closeSyncModal();
});

$("#btn-sync").addEventListener("click", openSyncModal);

$("#sync-gh-load-branches").addEventListener("click", async () => {
    const cfg = ghFormConfig();
    if (!cfg.repo) { alert("Enter a repo URL first."); return; }
    const btn = $("#sync-gh-load-branches");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
        const r = await api.listGitBranches(syncFolderId, {
            repo: cfg.repo,
            auth_method: cfg.auth_method,
            username: cfg.username,
            pat: cfg.pat,
            ssh_key: cfg.ssh_key,
        });
        const sel = $("#sync-gh-branches");
        const previouslySelected = new Set([...sel.selectedOptions].map((o) => o.value));
        sel.innerHTML = "";
        for (const b of r.branches) {
            const opt = document.createElement("option");
            opt.value = b;
            opt.textContent = b;
            opt.selected = previouslySelected.has(b);
            sel.append(opt);
        }
        if (sel.selectedOptions.length === 0 && sel.options.length > 0) {
            sel.options[0].selected = true; // default to first (main / master)
        }
    } catch (err) {
        alert(err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = prev;
    }
});

$("#sync-save").addEventListener("click", async () => {
    try {
        const out = await api.putSync(syncFolderId, syncBody());
        $("#sync-delete").hidden = false;
        renderSyncStatus(out);
        // Refresh credential placeholders: typed-in secrets are now stored
        // server-side, so we wipe the inputs (so a re-save doesn't re-send
        // them) and flip the placeholders to the "(saved — type to replace)"
        // wording. Same dance for both OAuth and SA panes — the user sees
        // "I just saved, the field is blank now" + "(saved — paste again to
        // replace)" instead of being unsure whether their paste persisted.
        if (out.source_type === "google_drive" && out.google_drive) {
            const gd = out.google_drive;
            $("#sync-gd-client-secret").value = "";
            $("#sync-gd-client-secret").placeholder = gd.has_client_secret
                ? "(saved — type to replace)"
                : "GOCSPX-…";
            $("#sync-gd-sa-json").value = "";
            $("#sync-gd-sa-json").placeholder = gd.has_service_account
                ? "(service account JSON saved — paste a new one to replace)"
                : '{"type":"service_account","client_email":"…","private_key":"…"}';
            // Folders the server now considers persisted — adopt them as
            // the canonical state so a subsequent Sync-now goes through
            // exactly what was saved (and we surface server-side renames
            // / dedupes in the next render).
            setGdFolders(gd.folders || []);
            setGdConnState({ connected: gd.connected, hasClientSecret: gd.has_client_secret });
        }
    } catch (err) {
        alert(err.message);
    }
});

$("#sync-trigger").addEventListener("click", async () => {
    // Always save first. Previously we only saved when no row existed,
    // which meant edits made after the initial save (toggling extended,
    // adding more folders to a Google Drive sync, …) were lost — the
    // trigger fired against the old config. Saving every time gives the
    // user the obvious "Sync now = sync what I see in this form" semantic.
    try {
        await api.putSync(syncFolderId, syncBody());
        await api.triggerSync(syncFolderId);
        alert("Sync queued. Watch the Recent jobs panel for progress.");
        closeSyncModal();
    } catch (err) {
        alert(err.message);
    }
});

// Google Drive: launch the OAuth flow in a popup. The callback closes its
// own tab and the server publishes folder.gd_connected; we re-load on
// modal focus to catch the new state without needing a websocket subscription.
$("#sync-gd-connect").addEventListener("click", async () => {
    try {
        // Save first so the server has client_id/client_secret to issue the
        // auth URL with.
        await api.putSync(syncFolderId, syncBody());
        const { auth_url } = await api.gdAuthInit(syncFolderId);
        const popup = window.open(auth_url, "voitta-gd-auth", "width=520,height=640");
        if (!popup) {
            alert("Popup blocked. Allow popups for this site and click Connect again.");
            return;
        }
        // Poll until the popup closes, then refresh the source.
        const t = setInterval(async () => {
            if (popup.closed) {
                clearInterval(t);
                await loadSyncSource();
            }
        }, 500);
    } catch (err) {
        alert(err.message);
    }
});

$("#sync-gd-pick-folder").addEventListener("click", async () => {
    try {
        const data = await api.gdListFolders(syncFolderId);
        await openGdPicker(data);
    } catch (err) {
        alert(err.message);
    }
});

// ----- Google Drive folder-picker modal -----

function openGdPicker(data) {
    const all = [
        ...data.folders.map((f) => ({ ...f, group: "My Drive" })),
        ...data.shared_folders.map((f) => ({ ...f, group: "Shared with me" })),
        ...data.shared_drives.map((f) => ({ ...f, group: "Shared drives" })),
    ];
    if (all.length === 0) {
        alert("No Drive folders found.");
        return;
    }
    const list = $("#gd-pick-list");
    list.innerHTML = "";
    const preselected = new Set(gdFolders.map((f) => f.id));

    // Group rows under a section heading so the user can scan visually.
    const groups = ["My Drive", "Shared with me", "Shared drives"];
    for (const group of groups) {
        const items = all.filter((f) => f.group === group);
        if (items.length === 0) continue;
        const header = document.createElement("div");
        header.style.cssText = "font-weight:600;margin:6px 0 2px;color:var(--muted, #666);font-size:12px;text-transform:uppercase;";
        header.textContent = group;
        list.append(header);
        for (const f of items) {
            const row = document.createElement("label");
            row.className = "check-row";
            row.style.cssText = "display:flex;gap:8px;align-items:flex-start;padding:4px 0;";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.dataset.id = f.id;
            cb.dataset.name = f.name;
            cb.checked = preselected.has(f.id);
            cb.style.marginTop = "3px";  // line up with the first text row
            const text = document.createElement("span");
            text.style.cssText = "display:flex;flex-direction:column;gap:1px;line-height:1.3;";
            const title = document.createElement("span");
            title.textContent = f.name;
            text.append(title);
            const subtitle = gdSubtitle(f);
            if (subtitle) {
                const sub = document.createElement("span");
                sub.style.cssText = "font-size:11px;color:var(--color-text-secondary);";
                sub.textContent = subtitle;
                text.append(sub);
            }
            row.append(cb, text);
            list.append(row);
        }
    }
    refreshGdPickAllState();
    $("#gd-pick-backdrop").hidden = false;
}

/* Build the dim subtitle line under each picker row.

   Eight folders all called "Meet Recordings" only become useful once you
   can see whose they are; this is also where we surface stale-folder
   age (`modifiedTime` on a Meet Recordings folder = the date of the last
   recording dropped in). Drive returns ISO timestamps; we render the
   date portion only because seconds add visual noise without information.
*/
function gdSubtitle(f) {
    const parts = [];
    if (f.owner_email) parts.push(f.owner_email);
    if (f.shared_at) parts.push(`shared ${f.shared_at.slice(0, 10)}`);
    if (f.modified_at) parts.push(`modified ${f.modified_at.slice(0, 10)}`);
    return parts.join(" · ");
}

function refreshGdPickAllState() {
    const boxes = $("#gd-pick-list").querySelectorAll('input[type="checkbox"]');
    const all = $("#gd-pick-all");
    if (boxes.length === 0) { all.checked = false; all.indeterminate = false; return; }
    let on = 0;
    boxes.forEach((b) => { if (b.checked) on += 1; });
    all.checked = on === boxes.length;
    all.indeterminate = on > 0 && on < boxes.length;
}

$("#gd-pick-list").addEventListener("change", refreshGdPickAllState);

$("#gd-pick-all").addEventListener("change", () => {
    const on = $("#gd-pick-all").checked;
    $("#gd-pick-list").querySelectorAll('input[type="checkbox"]').forEach((b) => { b.checked = on; });
});

function closeGdPicker() {
    $("#gd-pick-backdrop").hidden = true;
}

$("#gd-pick-close").addEventListener("click", closeGdPicker);
$("#gd-pick-cancel").addEventListener("click", closeGdPicker);
$("#gd-pick-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "gd-pick-backdrop") closeGdPicker();
});

$("#gd-pick-ok").addEventListener("click", () => {
    const picked = [];
    $("#gd-pick-list").querySelectorAll('input[type="checkbox"]:checked').forEach((b) => {
        picked.push({ id: b.dataset.id, name: b.dataset.name });
    });
    setGdFolders(picked);
    closeGdPicker();
});

$("#sync-delete").addEventListener("click", async () => {
    if (!confirm("Remove the sync configuration?\n\nFiles already on disk will not be deleted.")) return;
    try {
        await api.deleteSync(syncFolderId);
        closeSyncModal();
    } catch (err) {
        alert(err.message);
    }
});

// ----- Auth gate -----

async function ensureAuthenticated() {
    // Returns true when the user is signed in (or auth is bypassed via
    // VOITTA_SINGLE_USER / VOITTA_DEV_USER / forwarded headers); false when
    // we rendered the login gate and the rest of bootstrap should bail.
    try {
        const me = await api.me();
        $("#user-pill").textContent = me.email;
        $("#user-pill").hidden = false;
        $("#btn-logout").hidden = false;
        $("#login-gate").hidden = true;
        return true;
    } catch (err) {
        if (!String(err.message || "").startsWith("401")) {
            console.error("auth/me failed", err);
            return true; // not an auth issue — let the app try to load
        }
    }
    // 401: render the gate. Hide the Sign-in button if the server has no
    // Google client configured; the help text tells the operator how to fix.
    let cfg;
    try { cfg = await api.authConfig(); } catch { cfg = { google_enabled: false }; }
    if (!cfg.google_enabled) {
        $("#login-gate-google").hidden = true;
        $("#login-gate-disabled").hidden = false;
    }
    $("#login-gate").hidden = false;
    return false;
}

$("#btn-logout").addEventListener("click", async () => {
    try { await api.logout(); } catch (err) { console.warn("logout failed", err); }
    // Hard reload so any in-memory state (folders, files, ws connection) is
    // dropped and the gate re-renders cleanly.
    window.location.reload();
});

// ----- Settings modal -----

function openSettings() {
    $("#settings-backdrop").hidden = false;
    $("#key-reveal").hidden = true;
    $("#key-name").value = "";
    refreshKeys();
}

function closeSettings() {
    $("#settings-backdrop").hidden = true;
}

$("#user-pill").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", closeSettings);
$("#settings-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "settings-backdrop") closeSettings();
});

function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleString();
}

async function refreshKeys() {
    let keys = [];
    try {
        keys = await api.listKeys();
    } catch (err) {
        alert(err.message);
        return;
    }
    const tbody = $("#keys-tbody");
    tbody.innerHTML = "";
    $("#keys-empty").hidden = keys.length > 0;
    for (const k of keys) {
        const tr = document.createElement("tr");
        tr.style.borderBottom = "1px solid var(--border, #eee)";
        tr.innerHTML = `
            <td style="padding:6px 8px;">${escapeHtml(k.name)}</td>
            <td style="padding:6px 8px;font-family:monospace;">${escapeHtml(k.prefix)}…</td>
            <td style="padding:6px 8px;">${fmtTime(k.created_at)}</td>
            <td style="padding:6px 8px;">${fmtTime(k.last_used_at)}</td>
            <td style="padding:6px 8px;text-align:right;"></td>
        `;
        const del = document.createElement("button");
        del.className = "btn btn-secondary btn-sm";
        del.textContent = "Delete";
        del.addEventListener("click", () => deleteKey(k));
        tr.lastElementChild.append(del);
        tbody.append(tr);
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]
    ));
}

async function deleteKey(k) {
    if (!confirm(`Delete key "${k.name}"?\n\nAny client still using its token will stop working immediately.`)) return;
    try {
        await api.deleteKey(k.id);
        await refreshKeys();
    } catch (err) {
        alert(err.message);
    }
}

$("#key-create").addEventListener("click", async () => {
    const name = $("#key-name").value.trim();
    if (!name) { alert("Give the key a name first."); return; }
    let created;
    try {
        created = await api.createKey(name);
    } catch (err) {
        alert(err.message);
        return;
    }
    $("#key-name").value = "";
    $("#key-reveal-token").textContent = created.token;
    // Build copy-paste snippets pinned to the current origin so the user can
    // wire up Claude / Claude Desktop without assembling URLs by hand.
    const mcpUrl = `${window.location.origin}/mcp`;
    const claudeDesktop = JSON.stringify({
        mcpServers: {
            "voitta-image-rag": {
                type: "http",
                url: mcpUrl,
                headers: { Authorization: `Bearer ${created.token}` },
            },
        },
    }, null, 2);
    const cli = `claude mcp add --transport http voitta-image-rag ${mcpUrl} \\\n  --header "Authorization: Bearer ${created.token}"`;
    $("#key-reveal-claude").textContent = claudeDesktop;
    $("#key-reveal-cli").textContent = cli;
    $("#key-reveal").hidden = false;
    await refreshKeys();
});

$("#key-reveal-copy").addEventListener("click", async () => {
    const tok = $("#key-reveal-token").textContent;
    try {
        await navigator.clipboard.writeText(tok);
        const btn = $("#key-reveal-copy");
        const prev = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch {
        // Clipboard API can be blocked (insecure context, permissions). Fall
        // back to selecting the token so the user can copy with the keyboard.
        const node = $("#key-reveal-token");
        const range = document.createRange();
        range.selectNodeContents(node);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
    }
});

$("#key-reveal-dismiss").addEventListener("click", () => {
    $("#key-reveal").hidden = true;
    $("#key-reveal-token").textContent = "";
});

// ----- Bootstrap -----

async function bootstrap() {
    if (!(await ensureAuthenticated())) return;
    try {
        rootInfo = await api.root();
        const hint = $("#root-hint");
        hint.textContent = rootInfo.configured
            ? `Managed root: ${rootInfo.root_path}`
            : "VOITTA_ROOT_PATH not set — only 'Add existing' works.";
        $("#btn-new-folder").disabled = !rootInfo.configured;
        folders.set(await api.listFolders());
        files.set(await api.listAllFiles());
        jobs.set(await api.recentJobs());
    } catch (err) {
        console.warn("snapshot failed", err);
    }
    connect();
}

bootstrap();
