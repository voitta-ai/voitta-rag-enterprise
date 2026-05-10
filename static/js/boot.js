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
import { renderFolders } from "./render/tree.js";
import "./modals/admin.js";  // self-wires Admin button + admin modal
import { ensureAuthenticated } from "./modals/login.js";
import { setRootInfo } from "./modals/new-folder.js";
import "./modals/settings.js";  // self-wires user-pill click + Settings modal
import "./modals/sync.js";  // self-wires #btn-sync + sync modal + GD picker
import "./flows/upload.js";  // self-wires Upload button + file input
import { updateToolbarState } from "./flows/toolbar.js";
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
