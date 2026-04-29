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
        const del = document.createElement("button");
        del.textContent = "×";
        del.title = "remove";
        del.style.padding = "0 0.4rem";
        del.addEventListener("click", async () => {
            if (confirm(`Remove ${f.display_name}?`)) await api.deleteFolder(f.id);
        });
        li.append(label, del);
        ul.append(li);
    }
});

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
        const left = document.createElement("span");
        left.innerHTML = `<span class="kind">${j.kind}</span> #${j.id}`;
        const right = document.createElement("span");
        right.textContent = j.state;
        li.append(left, right);
        ul.append(li);
    }
});

$("#add-folder").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const path = fd.get("path");
    try {
        await api.addFolder(path);
        e.target.reset();
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

// Initial snapshot, then live updates via WS.
async function bootstrap() {
    try {
        folders.set(await api.listFolders());
        files.set(await api.listAllFiles());
        jobs.set(await api.recentJobs());
    } catch (err) {
        console.warn("snapshot failed", err);
    }
    connect();
}

bootstrap();
