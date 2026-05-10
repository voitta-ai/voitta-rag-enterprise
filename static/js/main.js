// SPA entry point. Folder-list driven, with a selection-aware sidebar.

import { api } from "./api.js";
import {
    scheduleFullRender,
    scheduleJobsRender,
    scheduleSidebarRender,
    setRenderers,
} from "./render/render-loop.js";
import { renderJobs } from "./render/jobs.js";
import { renderSidebar } from "./render/sidebar.js";
import { updateJobsTabIndicator } from "./render/tabs.js";
import { renderFolders } from "./render/tree.js";
import "./modals/admin.js";  // self-wires Admin button + admin modal
import { ensureAuthenticated } from "./modals/login.js";
import { closeModal, openModal, setRootInfo } from "./modals/new-folder.js";
import "./modals/settings.js";  // self-wires user-pill click + Settings modal
import "./modals/sync.js";  // self-wires #btn-sync + sync modal + GD picker
import {
    addGhostDir,
    expand,
    getSelectedFolderId,
    getSelectedRelDir,
    selectNode,
    setSelection,
} from "./flows/selection.js";
import { connStatus, files, folders, folderStats, jobs, reindexProgress, syncProgress } from "./store.js";
import { connect } from "./ws.js";

const $ = (sel) => document.querySelector(sel);


// ----- Connection pill -----
connStatus.subscribe((s) => {
    const el = $("#conn-status");
    el.textContent = s;
    el.className = `status-pill ${s}`;
});

// Wire the render functions into the leaf scheduler module so other
// modules can import scheduler functions without circular imports.
setRenderers({
    full: () => {
        renderFolders(folders.get());
        renderSidebar();
        updateToolbarState();
    },
    sidebar: () => renderSidebar(),
    jobs: () => renderJobs(),
});

// ----- Stores -----
folders.subscribe(() => {
    scheduleFullRender();
});
files.subscribe(() => {
    // Toolbar visibility depends on whether the selected folder has files,
    // which is computed from this store — handled inside scheduleFullRender.
    scheduleFullRender();
});
reindexProgress.subscribe(() => {
    // Progress events arrive at ~5/s during a wipe. The badge lives in
    // the sidebar only — the tree doesn't read progress state — so we
    // skip the tree rebuild entirely.
    scheduleSidebarRender();
});
syncProgress.subscribe(() => {
    scheduleSidebarRender();
});
folderStats.subscribe(() => {
    // Backend pushes folder.stats_changed coalesced per folder_id.
    // The sidebar is the only consumer (chunks / images / bytes /
    // by_extension / health badge), so a sidebar-only render is enough
    // — no need to rebuild the tree.
    scheduleSidebarRender();
});
jobs.subscribe(() => {
    scheduleJobsRender();
    updateJobsTabIndicator();
    // The tree's per-subtree status reads jobs.get() to decide between
    // "indexing" and "indexed" (see hasActiveWork in summariseSubtree). The
    // backend publishes file.upserted *before* the worker writes mark_done,
    // so when the last embed lands the file event arrives while the job is
    // still 'running' — and a moment later the job goes to 'done' but
    // nothing re-renders the tree. Re-render on jobs changes too so the
    // status flips to green without needing a manual expand/collapse.
    scheduleFullRender();
});


function updateToolbarState() {
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    const isRoot = !!folder && getSelectedRelDir() === "";
    // Read-only = a shared folder owned by someone else. Owner-only mutations
    // (upload, mkdir, reindex, sync, remove) are disabled; viewers can still
    // expand the tree and read files.
    const isOwned = !!(folder && folder.owned);
    const readOnly = !!folder && !isOwned;

    $("#btn-new-subfolder").disabled = !folder || readOnly;
    $("#btn-upload").disabled = !folder || readOnly;
    $("#btn-reindex").disabled = !folder || readOnly;
    // Sync button: only at the folder root. Hidden when the folder is
    // non-empty AND has no sync source — sync can't be configured on an
    // existing folder of files. When a sync source already exists, the
    // same button reads "Config" (re-opens the same modal).
    const syncBtn = $("#btn-sync");
    if (!folder || !isRoot || readOnly) {
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
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    if (!folder) return;
    const name = prompt("Subfolder name:");
    if (!name?.trim()) return;
    const target = getSelectedRelDir()
        ? `${getSelectedRelDir()}/${name.trim()}`
        : name.trim();
    try {
        await api.mkdir(folder.id, target);
        addGhostDir(folder.id, target);
        expand(folder.id, getSelectedRelDir());
        renderFolders(folders.get());
    } catch (err) {
        alert(err.message);
    }
}

// ----- Toolbar / sidebar actions -----

$("#btn-new-folder").addEventListener("click", openModal);
$("#modal-close").addEventListener("click", closeModal);
$("#modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
});

$("#btn-new-subfolder").addEventListener("click", createSubfolder);

$("#btn-upload").addEventListener("click", () => $("#upload-input").click());
$("#upload-input").addEventListener("change", async (e) => {
    const selected = Array.from(e.target.files);
    if (!selected.length || !getSelectedFolderId()) return;

    const wrap = $("#upload-progress");
    const fill = $("#upload-progress-fill");
    const label = $("#upload-progress-label");
    const list = $("#upload-file-list");

    // Per-file row: name + percent + state class. Bytes-loaded across all
    // files drives the aggregate bar so the user can also see total
    // throughput at a glance. Rows persist on completion and surface the
    // ✓ / × state — see issue #23 (visible per-file completion).
    const rows = selected.map((file) => {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = file.name;
        const pct = document.createElement("span");
        pct.className = "pct";
        pct.textContent = "0%";
        li.append(name, pct);
        list.append(li);
        return { file, li, pct, loaded: 0 };
    });
    const totalBytes = selected.reduce((sum, f) => sum + (f.size || 0), 0);
    wrap.hidden = false;
    fill.style.width = "0%";
    label.textContent = `Uploading ${selected.length} file(s)…`;

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
            selected,
            getSelectedRelDir(),
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
        // Leave the list visible long enough to read; clear once everything's
        // settled so the toolbar isn't permanently cluttered.
        setTimeout(() => {
            wrap.hidden = true;
            list.replaceChildren();
        }, failures.length ? 5000 : 1500);
    } catch (err) {
        wrap.hidden = true;
        list.replaceChildren();
        alert(err.message);
    } finally {
        e.target.value = "";
    }
});

$("#btn-reindex").addEventListener("click", async () => {
    if (!getSelectedFolderId()) return;
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    if (!folder) return;
    const allFolderFiles = files.get().filter(
        (x) => x.folder_id === folder.id && x.state !== "deleted",
    );
    const subtreeFiles = getSelectedRelDir()
        ? allFolderFiles.filter((f) => f.rel_path.startsWith(`${getSelectedRelDir()}/`))
        : allFolderFiles;
    if (subtreeFiles.length === 0) {
        alert("No files to reindex in this subtree.");
        return;
    }
    const where = getSelectedRelDir()
        ? `${folder.display_name}/${getSelectedRelDir()}`
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
        const r = await api.reindexFolder(getSelectedFolderId(), getSelectedRelDir());
        if (r.scheduled === 0) alert("No files were scheduled.");
    } catch (err) {
        alert(err.message);
    }
});

$("#btn-remove").addEventListener("click", async () => {
    if (!getSelectedFolderId()) return;
    const folder = folders.get().find((f) => f.id === getSelectedFolderId());
    if (!folder) return;
    if (!confirm(`Delete folder "${folder.display_name}"?\n\nThe folder and all its files will be permanently removed from disk.`)) return;
    try {
        await api.deleteFolder(getSelectedFolderId());
        setSelection(null, "");
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
$("#btn-kill-all").addEventListener("click", async () => {
    if (!confirm("Stop the running job and discard everything queued?")) return;
    try {
        const r = await api.cancelAllJobs();
        if (r.cancelled_queued === 0 && r.killed_running === 0) {
            alert("No running or queued jobs to kill");
        }
        // Refresh the panel so the user sees queued rows flip to done.
        jobs.set(await api.recentJobs());
    } catch (err) { alert(err.message); }
});


// ----- Bootstrap -----

async function bootstrap() {
    if (!(await ensureAuthenticated())) return;
    try {
        const rootInfo = await api.root();
        setRootInfo(rootInfo);
        const hint = $("#root-hint");
        hint.textContent = rootInfo.configured
            ? `Managed root: ${rootInfo.root_path}`
            : "VOITTA_ROOT_PATH not set — folder creation is disabled.";
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
