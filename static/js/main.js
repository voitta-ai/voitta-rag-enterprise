// SPA entry point. Folder-list driven, with a selection-aware sidebar.

import { api } from "./api.js";
import { connStatus, files, folders, jobs } from "./store.js";
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
    scheduleStatsRefresh();
});
jobs.subscribe(() => {
    renderJobs();
    // A job finishing usually means chunks/images counts moved.
    scheduleStatsRefresh();
});

function aggregateStatus(folderFiles) {
    if (folderFiles.length === 0) return "none";
    if (folderFiles.some((f) => f.state === "error")) return "error";
    if (folderFiles.every((f) => f.state === "indexed")) return "indexed";
    return "indexing";
}

// Collapse the indexer's internal substate vocabulary into three user-facing
// labels. The full transitions (pending → extracting → extracted → embedding)
// are mid-pipeline and not actionable for the user.
function userStateLabel(state) {
    if (state === "indexed" || state === "error" || state === "deleted") return state;
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

function summariseSubtree(node) {
    /* Aggregates file totals across the subtree rooted at node. */
    let total = 0, indexed = 0, errored = 0, pending = 0, embedding = 0;
    function walk(n) {
        for (const f of n.files) {
            total++;
            if (f.state === "indexed") indexed++;
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
        else if (indexed === total) status = "indexed";
        else if (embedding > 0) status = "indexing";
        else status = "pending";
    }
    return { total, indexed, errored, status };
}

// ---------- Tree rendering ----------

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
        });
    }
}

function renderTreeRow({ ul, folder, node, relDir, displayName, depth, isRoot }) {
    const summary = summariseSubtree(node);
    const key = nodeKey(folder.id, relDir);
    const hasChildren = node.dirs.size > 0 || node.files.length > 0;
    const isOpen = expandedNodes.has(key);
    const isSelected = folder.id === selectedFolderId && relDir === selectedRelDir;
    const canHaveChildren = isRoot || true; // dir nodes always

    const li = document.createElement("li");
    li.className = `tree-row ${isRoot ? "folder-root" : "dir"}` + (isSelected ? " selected" : "");
    li.dataset.key = key;

    const indent = document.createElement("span");
    indent.className = "indent";
    indent.style.width = `${depth * 16}px`;
    li.append(indent);

    const chevron = document.createElement("span");
    chevron.className = "chevron" + (isOpen ? " open" : "") + (hasChildren ? "" : " leaf");
    chevron.textContent = "▸";
    chevron.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!hasChildren) return;
        if (isOpen) expandedNodes.delete(key); else expandedNodes.add(key);
        renderFolders(folders.get());
    });
    li.append(chevron);

    const label = document.createElement("span");
    label.className = "label";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    glyph.textContent = isRoot ? "▣" : "📁";
    const text = document.createElement("span");
    text.textContent = displayName;
    label.append(glyph, text);
    li.append(label);

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

    const indent = document.createElement("span");
    indent.className = "indent";
    indent.style.width = `${depth * 16}px`;
    li.append(indent);

    const chevron = document.createElement("span");
    chevron.className = "chevron leaf";
    chevron.textContent = "·";
    li.append(chevron);

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
    li.append(label);

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

    $("#btn-new-subfolder").disabled = !isManaged;
    $("#btn-upload").disabled = !isManaged;
    $("#btn-reindex").disabled = !folder;
    $("#btn-remove").disabled = !isRoot;
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
    const pending = subtreeFiles.filter(
        (x) => x.state !== "indexed" && x.state !== "error",
    ).length;
    $("#kv-files").textContent = total;
    $("#kv-indexed").textContent = indexed;
    $("#kv-errors").textContent = errors;
    $("#kv-pending").textContent = pending;

    // Folder-level stats from /api/folders/{id}/stats — independent of subdir.
    const s = statsCache && statsCache.folder_id === folder.id ? statsCache : null;
    $("#kv-bytes").textContent = s ? humanBytes(s.bytes_total) : "…";
    $("#kv-chunks").textContent = s ? s.chunks_total : "…";
    $("#kv-images").textContent = s ? s.images_total : "…";
    $("#kv-images-unique").textContent = s ? s.images_unique : "…";

    const extTable = $("#ext-table");
    const extTbody = $("#ext-tbody");
    extTbody.innerHTML = "";
    const exts = s ? Object.entries(s.by_extension).sort((a, b) => b[1] - a[1]) : [];
    extTable.hidden = exts.length === 0;
    for (const [ext, count] of exts) {
        const tr = document.createElement("tr");
        const tdExt = document.createElement("td");
        tdExt.className = "ext";
        tdExt.textContent = ext;
        const tdCount = document.createElement("td");
        tdCount.className = "num";
        tdCount.textContent = count;
        tr.append(tdExt, tdCount);
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
    const file = e.target.files[0];
    if (!file || !selectedFolderId) return;
    try {
        await api.upload(selectedFolderId, file, selectedRelDir);
    } catch (err) {
        alert(err.message);
    } finally {
        e.target.value = "";
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

// ----- Bootstrap -----

async function bootstrap() {
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
