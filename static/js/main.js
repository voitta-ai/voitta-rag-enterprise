// SPA entry point. Folder-list driven, with a selection-aware sidebar.

import { api } from "./api.js";
import { connStatus, files, folders, jobs } from "./store.js";
import { connect } from "./ws.js";

const $ = (sel) => document.querySelector(sel);

let selectedFolderId = null;
let rootInfo = { configured: false, root_path: null };
let statsCache = null; // last successful FolderStats response
let statsTimer = null;

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
    if (folderFiles.some((f) => f.pending_embeds > 0 || f.state === "extracted" || f.state === "embedding")) return "indexing";
    return "pending";
}

function renderFolders(list) {
    const ul = $("#folder-list");
    ul.innerHTML = "";
    const sorted = [...list].sort((a, b) => a.id - b.id);
    if (sorted.length === 0) {
        const empty = document.createElement("li");
        empty.className = "folder-row";
        empty.style.gridTemplateColumns = "1fr";
        empty.style.color = "var(--color-text-secondary)";
        empty.textContent = "No folders yet — create or add one above.";
        ul.append(empty);
        return;
    }
    const allFiles = files.get();
    for (const f of sorted) {
        const folderFiles = allFiles.filter((x) => x.folder_id === f.id);
        const total = folderFiles.length;
        const indexed = folderFiles.filter((x) => x.state === "indexed").length;
        const errored = folderFiles.filter((x) => x.state === "error").length;
        const status = aggregateStatus(folderFiles);

        const li = document.createElement("li");
        li.className = "folder-row" + (f.id === selectedFolderId ? " selected" : "");
        li.dataset.id = f.id;
        li.addEventListener("click", () => selectFolder(f.id));

        const name = document.createElement("div");
        name.className = "name";
        name.innerHTML = `<span class="icon">▸</span><span class="label"></span>`;
        name.querySelector(".label").textContent = f.display_name;
        li.append(name);

        const fileCount = document.createElement("span");
        fileCount.className = "num";
        fileCount.textContent = total;
        li.append(fileCount);

        const indexedCount = document.createElement("span");
        indexedCount.className = "num";
        indexedCount.textContent = `${indexed}${errored ? ` · ${errored}!` : ""}`;
        li.append(indexedCount);

        const tag = document.createElement("span");
        tag.className = `status-tag ${status}`;
        tag.textContent = status;
        li.append(tag);

        const del = document.createElement("button");
        del.className = "delete";
        del.title = "Remove folder";
        del.textContent = "×";
        del.addEventListener("click", async (e) => {
            e.stopPropagation();
            if (!confirm(`Remove ${f.display_name}?`)) return;
            try {
                await api.deleteFolder(f.id);
                if (selectedFolderId === f.id) selectedFolderId = null;
            } catch (err) { alert(err.message); }
        });
        li.append(del);

        ul.append(li);
    }
}

// ----- Sidebar -----

function selectFolder(id) {
    selectedFolderId = id;
    statsCache = null;
    renderFolders(folders.get());
    renderSidebar();
    refreshStats();
}

function renderSidebar() {
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    const empty = $("#sidebar-empty");
    const detail = $("#folder-detail");
    const upload = $("#upload-card");
    const filesCard = $("#files-card");

    if (!folder) {
        empty.hidden = false;
        detail.hidden = true;
        upload.hidden = true;
        filesCard.hidden = true;
        return;
    }

    empty.hidden = true;
    detail.hidden = false;

    $("#folder-name").textContent = folder.display_name;
    $("#folder-path").textContent = folder.path;
    $("#folder-managed-badge").hidden = !folder.managed;
    $("#folder-source-badge").textContent = folder.source_type;

    const folderFiles = files.get().filter((x) => x.folder_id === folder.id);
    const total = folderFiles.length;
    const indexed = folderFiles.filter((x) => x.state === "indexed").length;
    const errors = folderFiles.filter((x) => x.state === "error").length;
    const pending = folderFiles.filter(
        (x) => x.state !== "indexed" && x.state !== "error" && x.state !== "deleted",
    ).length;
    $("#kv-files").textContent = total;
    $("#kv-indexed").textContent = indexed;
    $("#kv-errors").textContent = errors;
    $("#kv-pending").textContent = pending;

    // Stats from /api/folders/{id}/stats — populated lazily.
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

    upload.hidden = !folder.managed;
    filesCard.hidden = total === 0;
    if (total > 0) renderFiles(folderFiles);
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

function renderFiles(folderFiles) {
    const ul = $("#files");
    ul.innerHTML = "";
    const sorted = [...folderFiles].sort((a, b) => a.rel_path.localeCompare(b.rel_path));
    for (const f of sorted.slice(0, 100)) {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = f.rel_path;
        name.title = f.rel_path;
        const tag = document.createElement("span");
        tag.className = `status-tag ${f.state}`;
        tag.textContent = f.state;
        li.append(name, tag);
        ul.append(li);
    }
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

$("#btn-delete-folder").addEventListener("click", async () => {
    if (!selectedFolderId) return;
    const folder = folders.get().find((f) => f.id === selectedFolderId);
    if (!folder || !confirm(`Remove ${folder.display_name}?`)) return;
    try {
        await api.deleteFolder(selectedFolderId);
        selectedFolderId = null;
    } catch (err) { alert(err.message); }
});

$("#upload-submit").addEventListener("click", async () => {
    if (!selectedFolderId) return;
    const file = $("#upload-input").files[0];
    if (!file) return;
    try {
        await api.upload(selectedFolderId, file);
        $("#upload-input").value = "";
    } catch (err) { alert(err.message); }
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
