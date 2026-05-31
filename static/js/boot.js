// Application entry point.
//
// Side-effect import order matters: every module that wires DOM event
// handlers at module load (modals/*, flows/upload.js, flows/toolbar.js,
// render/tabs.js) is imported before bootstrap runs. By the time
// ``connect()`` opens the WebSocket every store subscriber and DOM
// listener is in place to consume events.

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
import { renderFoldersFiltered, setFolderFilter } from "./render/tree.js";
// Register preview plugins in priority order. Each import has a side
// effect of calling registerPlugin(); unsupported.js is always last.
import "./render/preview/plugins/image.js";
import "./render/preview/plugins/spreadsheet.js";
import "./render/preview/plugins/pages.js";
import "./render/preview/plugins/cad.js";
import "./render/preview/plugins/email.js";
import "./render/preview/plugins/text.js";
import "./render/preview/plugins/unsupported.js";
import "./modals/admin.js";  // self-wires Admin button + admin modal
import { ensureAuthenticated } from "./modals/login.js";
import { setRootInfo } from "./modals/new-folder.js";
import { setRenameRootInfo } from "./modals/rename-folder.js";  // self-wires #rename-* modal
import "./modals/settings.js";  // self-wires user-pill click + Settings modal
import "./modals/sync.js";  // self-wires #btn-sync + sync modal + GD picker
import "./flows/upload.js";  // self-wires Upload button + file input
import { updateToolbarState } from "./flows/toolbar.js";
import { activeFolders, connStatus, files, folders, folderStats, jobs, reindexProgress, syncProgress } from "./store.js";
import { addGhostDir } from "./flows/selection.js";
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
        renderFoldersFiltered();
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
    // skip the tree rebuild entirely. We also refresh the Jobs panel so
    // the inline phase suffix on the running reindex_folder row
    // ("Reindex folder  #14041 — wiping 800/1613") advances in lockstep.
    scheduleSidebarRender();
    scheduleJobsRender();
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
activeFolders.subscribe(() => {
    // Drives the per-row "indexing" pill in the tree (see
    // summariseSubtree in flows/tree-model.js). The set is small and
    // changes at queue-event rate, not per file, so a full render is
    // cheap and correctly re-applies the pill to every visible row.
    scheduleFullRender();
});
// 1-second tick: advances the "running 12s" tail on each Jobs-panel row.
// We don't want a permanent setInterval ticking when nothing is running
// (the panel would needlessly re-render every second forever) so the tick
// self-suspends whenever the jobs store has zero running rows, and the
// jobs.subscribe handler below revives it the moment one appears.
let _jobsTickHandle = null;
function _jobsTickerStartIfNeeded() {
    if (_jobsTickHandle !== null) return;
    if (!jobs.get().some((j) => j.state === "running")) return;
    _jobsTickHandle = setInterval(() => {
        if (!jobs.get().some((j) => j.state === "running")) {
            clearInterval(_jobsTickHandle);
            _jobsTickHandle = null;
            return;
        }
        scheduleJobsRender();
    }, 1000);
}

jobs.subscribe(() => {
    scheduleJobsRender();
    _jobsTickerStartIfNeeded();
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

// ----- Bootstrap -----

function hideBootOverlay() {
    const el = $("#boot-overlay");
    if (!el) return;
    el.classList.add("hidden");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
}

async function bootstrap() {
    if (!(await ensureAuthenticated())) {
        hideBootOverlay();
        return;
    }
    try {
        const rootInfo = await api.root();
        setRootInfo(rootInfo);
        setRenameRootInfo(rootInfo);
        $("#btn-new-folder").disabled = !rootInfo.configured;
        const folderList = await api.listFolders();
        folders.set(folderList);
        files.set(await api.listAllFiles());
        jobs.set(await api.recentJobs());
        // Seed the active-folders set so the tree pills are correct
        // before the first folder.active_changed event arrives. The
        // call is cheap (server returns a flat list of ids), so we
        // fold any error into a logged warning rather than blocking
        // the rest of the snapshot.
        try {
            activeFolders.set(new Set(await api.activeFolderIds()));
        } catch (err) {
            console.warn("active-ids seed failed", err);
        }
        // Seed ghost dirs from the real filesystem so empty subdirectories
        // appear in the tree even before any files are indexed into them.
        await Promise.allSettled(
            folderList.map(async (f) => {
                try {
                    const dirs = await api.listFolderDirs(f.id);
                    for (const rel of dirs) addGhostDir(f.id, rel);
                } catch (_) { /* best-effort */ }
            })
        );
    } catch (err) {
        console.warn("snapshot failed", err);
    }
    hideBootOverlay();
    connect();
}

// ----- Folder search -----

const folderSearchInput = $("#folder-search");
if (folderSearchInput) {
    folderSearchInput.addEventListener("input", () => {
        setFolderFilter(folderSearchInput.value);
        renderFoldersFiltered();
    });
}

bootstrap();
