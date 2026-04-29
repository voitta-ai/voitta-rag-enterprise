// Entry point. Wires the stores into DOM updates and form handlers.

import { api } from "./api.js";
import { connStatus, files, folders, jobs } from "./store.js";
import { connect } from "./ws.js";

const $ = (sel) => document.querySelector(sel);

connStatus.subscribe((s) => {
    const el = $("#conn-status");
    el.textContent = s;
    el.className = `status ${s}`;
});

folders.subscribe((list) => {
    const ul = $("#folder-list");
    ul.innerHTML = "";
    for (const f of [...list].sort((a, b) => a.id - b.id)) {
        const li = document.createElement("li");
        const label = document.createElement("span");
        label.textContent = f.display_name || f.path;
        label.title = f.path;
        const right = document.createElement("span");
        right.style.display = "flex";
        right.style.gap = "0.25rem";
        if (f.managed) {
            const tag = document.createElement("span");
            tag.className = "badge managed";
            tag.textContent = "managed";
            right.append(tag);
        }
        const del = document.createElement("button");
        del.textContent = "×";
        del.title = "remove";
        del.style.padding = "0 0.4rem";
        del.addEventListener("click", async () => {
            if (confirm(`Remove ${f.display_name}?`)) await api.deleteFolder(f.id);
        });
        right.append(del);
        li.append(label, right);
        ul.append(li);
    }
    refreshUploadFolderOptions(list);
});

function refreshUploadFolderOptions(list) {
    const sel = $("#upload-folder");
    const form = $("#upload-form");
    const managed = list.filter((f) => f.managed);
    sel.innerHTML = "";
    for (const f of managed) {
        const opt = document.createElement("option");
        opt.value = f.id;
        opt.textContent = f.display_name;
        sel.append(opt);
    }
    form.hidden = managed.length === 0;
}

files.subscribe((list) => {
    const ul = $("#file-list");
    ul.innerHTML = "";
    const sorted = [...list].sort((a, b) => a.rel_path.localeCompare(b.rel_path));
    for (const f of sorted.slice(0, 200)) {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = f.rel_path;
        name.title = `id=${f.id}`;
        const badge = document.createElement("span");
        badge.className = `badge ${f.state}`;
        badge.textContent = f.state;
        li.append(name, badge);
        ul.append(li);
    }
});

jobs.subscribe((list) => {
    const ul = $("#job-list");
    ul.innerHTML = "";
    for (const j of list.slice(0, 30)) {
        const li = document.createElement("li");
        li.className = `job ${j.state}`;
        const col = document.createElement("div");
        col.className = "col";
        const top = document.createElement("span");
        top.innerHTML = `<span class="kind">${j.kind}</span> #${j.id} — ${j.state}`;
        col.append(top);
        if (j.state === "error" && j.error) {
            const err = document.createElement("span");
            err.className = "job-error-msg";
            err.textContent = j.error;
            col.append(err);
        }
        li.append(col);
        if (j.state === "error") {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "job-retry";
            btn.textContent = "↻";
            btn.title = "retry";
            btn.addEventListener("click", async () => {
                try {
                    await api.retryJob(j.id);
                } catch (err) {
                    alert(err.message);
                }
            });
            li.append(btn);
        }
        ul.append(li);
    }
});

$("#job-retry-all").addEventListener("click", async () => {
    try {
        const r = await api.retryAllFailed();
        if (r.retried === 0) alert("No failed jobs to retry");
    } catch (err) {
        alert(err.message);
    }
});

$("#job-cleanup").addEventListener("click", async () => {
    if (!confirm("Permanently delete all failed-job records?")) return;
    try {
        const r = await api.cleanupFailedJobs();
        // refresh listing
        jobs.set(await api.recentJobs());
        if (r.retried) console.log(`removed ${r.retried} failed job(s)`);
    } catch (err) {
        alert(err.message);
    }
});

// Tabbed folder-create form: managed (name) vs external (picker).
const tabs = document.querySelectorAll(".tab");
const formManaged = $("#add-folder-managed");
const pickerEl = $("#add-folder-external");
let pickerInitialized = false;

tabs.forEach((t) => {
    t.addEventListener("click", () => {
        tabs.forEach((x) => x.classList.toggle("active", x === t));
        const mode = t.dataset.mode;
        formManaged.hidden = mode !== "managed";
        pickerEl.hidden = mode !== "external";
        if (mode === "external" && !pickerInitialized) {
            pickerInitialized = true;
            pickerNavigate(null);
        }
    });
});

formManaged.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name");
    try {
        await api.addFolderByName(name);
        e.target.reset();
    } catch (err) {
        alert(err.message);
    }
});

// --- Folder picker --------------------------------------------------
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
    } catch (err) {
        alert(err.message);
    }
}

$("#picker-up").addEventListener("click", () => {
    const target = $("#picker-up").dataset.target;
    if (target) pickerNavigate(target);
});

$("#picker-pick").addEventListener("click", async () => {
    if (!pickerCwd) return;
    try {
        await api.addFolderByPath(pickerCwd);
    } catch (err) {
        alert(err.message);
    }
});

$("#upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const folderId = $("#upload-folder").value;
    const file = $("#upload-input").files[0];
    if (!file) return;
    try {
        await api.upload(folderId, file);
        $("#upload-input").value = "";
    } catch (err) {
        alert(err.message);
    }
});

$("#search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = new FormData(e.target).get("query");
    const out = $("#search-results");
    out.textContent = "searching...";
    try {
        const res = await api.search(query, ["chunks", "images"]);
        renderHits(out, res);
    } catch (err) {
        out.textContent = err.message;
    }
});

function renderHits(out, res) {
    out.innerHTML = "";
    if (!res.chunks.length && !res.images.length) {
        out.textContent = "(no hits)";
        return;
    }
    for (const h of res.chunks) {
        const div = document.createElement("div");
        div.className = "hit";
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `chunk #${h.payload.chunk_index} • ${h.payload.file_path} • score ${h.score.toFixed(4)}`;
        const text = document.createElement("div");
        text.className = "text";
        text.textContent = (h.payload.text || "").slice(0, 600);
        div.append(meta, text);
        out.append(div);
    }
    for (const h of res.images) {
        const div = document.createElement("div");
        div.className = "hit";
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `image #${h.id} • ${h.payload.file_path} • score ${h.score.toFixed(4)}`;
        const img = document.createElement("img");
        img.src = `/api/images/${h.id}`;
        img.style.maxWidth = "256px";
        img.style.maxHeight = "256px";
        img.style.display = "block";
        img.style.marginTop = "0.5rem";
        div.append(meta, img);
        out.append(div);
    }
}

async function applyRoot() {
    const hint = $("#root-hint");
    try {
        const r = await api.root();
        if (r.configured) {
            hint.textContent = `Managed root: ${r.root_path}`;
        } else {
            hint.textContent = "VOITTA_ROOT_PATH not set — only 'Add existing' works.";
            // Auto-switch tab to external when root isn't configured.
            const ext = document.querySelector('.tab[data-mode="external"]');
            if (ext) ext.click();
            const managedTab = document.querySelector('.tab[data-mode="managed"]');
            if (managedTab) managedTab.disabled = true;
        }
    } catch (err) {
        hint.textContent = `(${err.message})`;
    }
}

// Initial snapshot, then live updates via WS.
async function bootstrap() {
    try {
        await applyRoot();
        folders.set(await api.listFolders());
        files.set(await api.listAllFiles());
        jobs.set(await api.recentJobs());
    } catch (err) {
        console.warn("snapshot failed", err);
    }
    connect();
}

bootstrap();
