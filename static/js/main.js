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
import { closeModal, openModal, setRootInfo } from "./modals/new-folder.js";
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

// In-memory cache of the Google providers fetched from
// /api/admin/auth-providers. Populated when the GD sync modal opens
// (refreshGdProviderPicker) and consulted whenever the picker fires
// onchange — keeps the populate path synchronous so users see the
// fields fill the instant they pick a row, with no flicker.
//
// Map<id, {id, label, client_id, client_secret, source}>.
const gdGoogleProviders = new Map();

async function refreshGdProviderPicker() {
    const row = $("#sync-gd-provider-row");
    const sel = $("#sync-gd-provider-picker");
    sel.innerHTML = '<option value="">— Manual entry —</option>';
    gdGoogleProviders.clear();

    let providers;
    try {
        providers = await api.adminListAuthProviders();
    } catch (err) {
        // 403 = not admin → silently keep the picker hidden so the
        // existing manual-entry flow is unchanged. Anything else, log
        // for diagnostics but keep going.
        if (!String(err.message || "").startsWith("403")) {
            console.warn("provider list failed", err);
        }
        row.hidden = true;
        return;
    }

    const enabledGoogle = (providers || []).filter(
        (p) => p.provider === "google" && p.enabled,
    );
    if (!enabledGoogle.length) {
        row.hidden = true;
        return;
    }

    for (const p of enabledGoogle) {
        gdGoogleProviders.set(p.id, p);
        const opt = document.createElement("option");
        opt.value = String(p.id);
        const tail = p.source === "env" ? "  (.env)" : "";
        opt.textContent = (p.label || p.client_id) + tail;
        sel.append(opt);
    }
    row.hidden = false;
}

function applyGdProviderSelection(providerId) {
    const pane = $("#sync-gd-pane-oauth");
    const idInput = $("#sync-gd-client-id");
    const secretInput = $("#sync-gd-client-secret");
    if (!providerId) {
        pane.classList.remove("provider-picked");
        // Manual entry — leave whatever the user has typed alone.
        return;
    }
    const p = gdGoogleProviders.get(Number(providerId));
    if (!p) return;
    idInput.value = p.client_id || "";
    secretInput.value = p.client_secret || "";
    // ``input`` events drive setGdConnState so the Connect button can
    // unlock; fire them so picking a provider feels identical to typing
    // the values manually.
    idInput.dispatchEvent(new Event("input", { bubbles: true }));
    secretInput.dispatchEvent(new Event("input", { bubbles: true }));
    pane.classList.add("provider-picked");
}

function preselectGdProviderByClientId(clientId) {
    const sel = $("#sync-gd-provider-picker");
    if (!clientId) { sel.value = ""; return; }
    for (const p of gdGoogleProviders.values()) {
        if (p.client_id === clientId) {
            sel.value = String(p.id);
            $("#sync-gd-pane-oauth").classList.add("provider-picked");
            return;
        }
    }
    sel.value = ""; // existing config doesn't match a saved provider
}

function openSyncModal() {
    if (!getSelectedFolderId()) return;
    syncFolderId = getSelectedFolderId();
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
    // Auto-sync defaults: off, 6h. loadSyncSource overrides from the row.
    $("#sync-auto-enabled").checked = false;
    $("#sync-auto-hours").value = "6";
    $("#sync-auto-hours").disabled = true;

    // Reset picker state, then refresh + load in sequence so the
    // pre-select-by-client_id pass in loadSyncSource sees a populated
    // gdGoogleProviders map.
    $("#sync-gd-provider-picker").value = "";
    $("#sync-gd-pane-oauth").classList.remove("provider-picked");
    refreshGdProviderPicker()
        .finally(loadSyncSource)
        .finally(() => {
            // If loadSyncSource didn't match a provider (no saved config,
            // or saved client_id that doesn't match a row) AND there's
            // exactly one enabled Google provider, auto-pick it. Saves a
            // click in the common single-provider case without
            // overriding an existing sync's saved value.
            const sel = $("#sync-gd-provider-picker");
            const idInput = $("#sync-gd-client-id");
            if (!sel.value && !idInput.value && gdGoogleProviders.size === 1) {
                const onlyId = [...gdGoogleProviders.keys()][0];
                sel.value = String(onlyId);
                applyGdProviderSelection(onlyId);
            }
        });
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
            preselectGdProviderByClientId(gd.client_id || "");
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
        // Auto-sync schedule (common to both source types).
        $("#sync-auto-enabled").checked = !!src.auto_sync_enabled;
        const hrs = Math.max(1, Math.min(24, Number(src.auto_sync_hours) || 6));
        $("#sync-auto-hours").value = String(hrs);
        $("#sync-auto-hours").disabled = !src.auto_sync_enabled;
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

    // Status / last-synced live in one row. The error message can be
    // multi-line (the GoogleWorkspaceAccessError preflight emits one
    // bullet per disabled API with its activation URL); we render it
    // in its own block below the row so newlines and URLs survive.
    line.innerHTML = "";
    const top = document.createElement("div");
    const parts = [`status: ${src.sync_status}`];
    if (src.last_synced_at) {
        const d = new Date(src.last_synced_at * 1000);
        parts.push(`last: ${d.toLocaleString()}`);
    }
    top.textContent = parts.join(" · ");
    line.appendChild(top);

    if (src.sync_error) {
        const errBlock = document.createElement("pre");
        errBlock.className = "sync-error-block";
        errBlock.textContent = src.sync_error;
        // Detect URLs in the message and turn them into anchors so the
        // user can click straight into the GCP "Enable API" page.
        // ``<pre>`` preserves newlines / spacing; we rebuild content
        // with link nodes interleaved.
        const urlRe = /(https?:\/\/[^\s)]+)/g;
        errBlock.innerHTML = "";
        let last = 0;
        let m;
        while ((m = urlRe.exec(src.sync_error)) !== null) {
            if (m.index > last) {
                errBlock.appendChild(document.createTextNode(src.sync_error.slice(last, m.index)));
            }
            const a = document.createElement("a");
            a.href = m[1];
            a.target = "_blank";
            a.rel = "noopener";
            a.textContent = m[1];
            errBlock.appendChild(a);
            last = m.index + m[1].length;
        }
        if (last < src.sync_error.length) {
            errBlock.appendChild(document.createTextNode(src.sync_error.slice(last)));
        }
        line.appendChild(errBlock);
    }
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
    // Auto-sync settings live alongside source_type — same payload for
    // both github and google_drive so the backend doesn't need source-
    // specific scheduling code.
    const autoEnabled = $("#sync-auto-enabled").checked;
    const autoHours = Math.max(1, Math.min(24, Number($("#sync-auto-hours").value) || 6));
    const base = { auto_sync_enabled: autoEnabled, auto_sync_hours: autoHours };
    if (t === "github") return { ...base, source_type: "github", github: ghFormConfig() };
    if (t === "google_drive") return { ...base, source_type: "google_drive", google_drive: gdFormConfig() };
    throw new Error(`Unknown source_type: ${t}`);
}

$("#sync-type").addEventListener("change", () => setSyncType($("#sync-type").value));

// Populate the auto-sync hours dropdown once on script load. Bounded
// 1-24 — the in-process scheduler is hour-grained; wider intervals
// belong to a real cron, finer would burn Drive quota uselessly since
// the watcher already picks up newly-arrived files once they land on
// disk.
(() => {
    const sel = $("#sync-auto-hours");
    if (!sel || sel.options.length > 0) return;
    for (let h = 1; h <= 24; h++) {
        const opt = document.createElement("option");
        opt.value = String(h);
        opt.textContent = String(h);
        sel.append(opt);
    }
    sel.value = "6";
})();

// Toggle disables the dropdown so a configured-but-disabled row keeps
// its hours setting visible (instead of resetting to default the next
// time the user re-enables it).
$("#sync-auto-enabled").addEventListener("change", () => {
    $("#sync-auto-hours").disabled = !$("#sync-auto-enabled").checked;
});

document.querySelectorAll('input[name="sync-gh-auth"]').forEach((el) => {
    el.addEventListener("change", () => setGhAuth(el.value));
});

// Provider picker — populating the credential fields from a saved
// row. Manual edits afterwards are preserved (Save uses the inputs
// verbatim); the accent border on the inputs comes from the
// ``provider-picked`` class set in applyGdProviderSelection.
$("#sync-gd-provider-picker").addEventListener("change", (e) => {
    applyGdProviderSelection(e.target.value);
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
        // Admin button is gated on the *real* user's flag — impersonation
        // never grants admin powers. The /api/auth/me endpoint enforces
        // the same: ``is_admin`` reflects the real identity.
        $("#btn-admin").hidden = !me.is_admin;
        // Impersonation banner: visible iff an admin has chosen "view as".
        if (me.acting_as_user_id) {
            $("#impersonate-text").textContent =
                `Viewing the app as ${me.acting_as_email} — your own admin status is unaffected.`;
            $("#impersonate-banner").hidden = false;
        } else {
            $("#impersonate-banner").hidden = true;
        }
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
            "voitta-rag-enterprise": {
                type: "http",
                url: mcpUrl,
                headers: { Authorization: `Bearer ${created.token}` },
            },
        },
    }, null, 2);
    const cli = `claude mcp add --transport http voitta-rag-enterprise ${mcpUrl} \\\n  --header "Authorization: Bearer ${created.token}"`;
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

// ----- Admin modal -----

function openAdmin() {
    $("#admin-backdrop").hidden = false;
    refreshAdmin();
}

function closeAdmin() {
    $("#admin-backdrop").hidden = true;
}

// Admin modal tabs — Sign-in gate / Users / OAuth providers. Pure DOM
// toggle; refreshAdmin always pulls all three sections regardless of
// which tab is visible, so flipping tabs is instant.
function setAdminTab(name) {
    if (!["access", "users", "oauth"].includes(name)) name = "access";
    for (const t of ["access", "users", "oauth"]) {
        const btn = $(`#admin-tab-btn-${t}`);
        const pane = $(`#admin-tab-pane-${t}`);
        if (!btn || !pane) continue;
        const active = t === name;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-selected", String(active));
        pane.hidden = !active;
    }
}
$("#admin-tab-btn-access").addEventListener("click", () => setAdminTab("access"));
$("#admin-tab-btn-users").addEventListener("click", () => setAdminTab("users"));
$("#admin-tab-btn-oauth").addEventListener("click", () => setAdminTab("oauth"));

async function refreshAdmin() {
    try {
        const [allow, users, providers] = await Promise.all([
            api.adminAllowlist(),
            api.adminListUsers(),
            api.adminListAuthProviders(),
        ]);
        renderList("#admin-domains", allow.domains, "domain", api.adminRemoveDomain);
        renderList("#admin-blocked", allow.blocked, "email", api.adminUnblock);
        renderUsersTable(users);
        renderAuthProvidersTable(providers);
    } catch (err) {
        alert(err.message);
    }
}

function renderAuthProvidersTable(providers) {
    const tbody = $("#admin-auth-providers-table tbody");
    const empty = $("#admin-auth-providers-empty");
    tbody.innerHTML = "";
    if (!providers.length) {
        empty.hidden = false;
        return;
    }
    empty.hidden = true;
    for (const p of providers) {
        tbody.appendChild(buildAuthProviderRow(p));
    }
}

// One row in the auth providers table. Inputs are live-editable; changes
// fire PATCH requests on blur (or Enter), so the admin can correct a
// pasted client_id without an explicit "save" click. The Check button
// rolls a credential probe through the backend; a small status pill
// appears next to it for ~5s.
function buildAuthProviderRow(p) {
    const tr = document.createElement("tr");
    tr.dataset.providerId = String(p.id);

    // Provider name (read-only after creation; switching providers would
    // be a different OAuth flow entirely).
    const tdProvider = document.createElement("td");
    tdProvider.textContent = p.provider;
    if (p.source === "env") {
        const badge = document.createElement("span");
        badge.className = "badge-super";
        badge.style.background = "#3b82f6";
        badge.textContent = ".env";
        badge.title = "Seeded from .env on startup. Deleting this row only sticks until the next restart while the env vars remain set.";
        tdProvider.appendChild(badge);
    }
    tr.appendChild(tdProvider);

    // Label, client_id, client_secret — inline editors.
    const tdLabel = document.createElement("td");
    tdLabel.appendChild(buildAuthProviderInput(p.id, "label", p.label, "Label"));
    tr.appendChild(tdLabel);

    const tdClientId = document.createElement("td");
    tdClientId.appendChild(buildAuthProviderInput(p.id, "client_id", p.client_id, "Client ID"));
    tr.appendChild(tdClientId);

    const tdSecret = document.createElement("td");
    tdSecret.appendChild(buildAuthProviderInput(p.id, "client_secret", p.client_secret, "Client secret"));
    tr.appendChild(tdSecret);

    // Enabled toggle.
    const tdEnabled = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!p.enabled;
    cb.title = p.enabled ? "Disable" : "Enable";
    cb.addEventListener("change", async () => {
        try {
            await api.adminUpdateAuthProvider(p.id, { enabled: cb.checked });
        } catch (err) {
            cb.checked = !cb.checked;
            alert(err.message);
        }
    });
    tdEnabled.appendChild(cb);
    tr.appendChild(tdEnabled);

    // Actions: Check + Delete.
    const tdActions = document.createElement("td");
    tdActions.style.whiteSpace = "nowrap";
    const checkBtn = document.createElement("button");
    checkBtn.className = "btn btn-secondary btn-sm";
    checkBtn.textContent = "Check";
    checkBtn.title = "Probe the provider's token endpoint to verify these credentials";
    const checkStatus = document.createElement("span");
    checkStatus.className = "hint";
    checkStatus.style.marginLeft = "8px";
    checkBtn.addEventListener("click", async () => {
        checkBtn.disabled = true;
        checkStatus.textContent = "Checking…";
        checkStatus.style.color = "";
        try {
            const r = await api.adminCheckAuthProvider(p.id);
            checkStatus.textContent = (r.ok ? "✓ " : "✗ ") + r.message;
            checkStatus.style.color = r.ok ? "#10b981" : "#dc3545";
        } catch (err) {
            checkStatus.textContent = "✗ " + (err.message || "request failed");
            checkStatus.style.color = "#dc3545";
        } finally {
            checkBtn.disabled = false;
            // Auto-clear after a beat so the row doesn't stay loud.
            setTimeout(() => { checkStatus.textContent = ""; }, 8000);
        }
    });
    const delBtn = document.createElement("button");
    delBtn.className = "btn-remove";
    delBtn.textContent = "×";
    delBtn.title = p.source === "env"
        ? "Delete (will be re-created on next restart while .env still has these values)"
        : "Delete";
    delBtn.style.marginLeft = "8px";
    delBtn.addEventListener("click", async () => {
        if (!confirm(`Delete ${p.provider} provider "${p.label || p.client_id}"?`)) return;
        try {
            await api.adminDeleteAuthProvider(p.id);
            await refreshAdmin();
        } catch (err) {
            alert(err.message);
        }
    });
    tdActions.append(checkBtn, checkStatus, delBtn);
    tr.appendChild(tdActions);

    return tr;
}

function buildAuthProviderInput(providerId, field, value, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.placeholder = placeholder;
    input.style.width = "100%";
    input.style.minWidth = field === "client_id" ? "240px" : "120px";
    let original = value || "";
    const commit = async () => {
        if (input.value === original) return;
        try {
            await api.adminUpdateAuthProvider(providerId, { [field]: input.value });
            original = input.value;
        } catch (err) {
            input.value = original; // revert
            alert(err.message);
        }
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = original; input.blur(); }
    });
    return input;
}

async function submitAddAuthProvider() {
    const provider = $("#admin-auth-provider-type").value;
    const label = $("#admin-auth-provider-label").value.trim();
    const clientId = $("#admin-auth-provider-client-id").value.trim();
    const clientSecret = $("#admin-auth-provider-client-secret").value;
    if (!clientId) { alert("Client ID is required"); return; }
    try {
        await api.adminCreateAuthProvider({
            provider,
            label,
            client_id: clientId,
            client_secret: clientSecret,
            enabled: true,
        });
        $("#admin-auth-provider-label").value = "";
        $("#admin-auth-provider-client-id").value = "";
        $("#admin-auth-provider-client-secret").value = "";
        await refreshAdmin();
    } catch (err) {
        alert(err.message);
    }
}

$("#admin-auth-provider-add").addEventListener("click", submitAddAuthProvider);
$("#admin-auth-provider-client-secret").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitAddAuthProvider(); }
});

function renderList(sel, items, _kind, removeFn) {
    const ul = $(sel);
    ul.innerHTML = "";
    if (!items.length) {
        const li = document.createElement("li");
        li.className = "empty";
        li.textContent = "(none)";
        ul.appendChild(li);
        return;
    }
    for (const v of items) {
        const li = document.createElement("li");
        const span = document.createElement("span");
        span.textContent = v;
        const btn = document.createElement("button");
        btn.className = "btn-remove";
        btn.title = "Remove";
        btn.textContent = "×";
        btn.addEventListener("click", async () => {
            try { await removeFn(v); await refreshAdmin(); }
            catch (err) { alert(err.message); }
        });
        li.appendChild(span);
        li.appendChild(btn);
        ul.appendChild(li);
    }
}

function renderUsersTable(users) {
    const tbody = $("#admin-users-table tbody");
    tbody.innerHTML = "";
    for (const u of users) {
        const tr = document.createElement("tr");

        const tdEmail = document.createElement("td");
        tdEmail.textContent = u.email;
        if (u.is_super_admin) {
            const badge = document.createElement("span");
            badge.className = "badge-super";
            badge.textContent = "SUPER";
            badge.title = "From VOITTA_SUPER_ADMINS — can't be demoted via UI.";
            tdEmail.appendChild(badge);
        }
        tr.appendChild(tdEmail);

        const tdName = document.createElement("td");
        tdName.textContent = u.display_name || "—";
        tr.appendChild(tdName);

        const tdAdmin = document.createElement("td");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = u.is_admin;
        cb.disabled = u.is_super_admin;
        cb.addEventListener("change", async () => {
            try { await api.adminSetIsAdmin(u.id, cb.checked); }
            catch (err) { alert(err.message); cb.checked = !cb.checked; }
        });
        tdAdmin.appendChild(cb);
        tr.appendChild(tdAdmin);

        const tdActions = document.createElement("td");
        tdActions.className = "row-actions";
        const viewBtn = document.createElement("button");
        viewBtn.className = "btn btn-secondary btn-sm";
        viewBtn.textContent = "View as";
        viewBtn.addEventListener("click", async () => {
            try {
                await api.adminImpersonate(u.id);
                window.location.reload();
            } catch (err) { alert(err.message); }
        });
        tdActions.appendChild(viewBtn);
        tr.appendChild(tdActions);

        tbody.appendChild(tr);
    }
}

$("#btn-admin").addEventListener("click", openAdmin);
$("#admin-close").addEventListener("click", closeAdmin);
$("#admin-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "admin-backdrop") closeAdmin();
});

// Wire each input + button pair so click and Enter both submit. Without
// the Enter binding the form looked broken to anyone who typed and hit
// return — a real bug report from the first admin-UI session.
function wireAdminAdd(inputSel, buttonSel, apiFn) {
    const input = $(inputSel);
    const submit = async () => {
        const v = input.value.trim();
        if (!v) return;
        try {
            await apiFn(v);
            input.value = "";
            await refreshAdmin();
        } catch (err) {
            alert(err.message);
        }
    };
    $(buttonSel).addEventListener("click", submit);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            submit();
        }
    });
}

wireAdminAdd("#admin-domain-input", "#admin-domain-add", api.adminAddDomain);
wireAdminAdd("#admin-block-input", "#admin-block-add", api.adminBlock);

// Pre-create user (with optional admin grant). Different shape from the
// allowlist add rows because it has an extra "Admin" checkbox alongside
// the email input — wireAdminAdd only handles single-input.
async function submitAddUser() {
    const input = $("#admin-newuser-input");
    const adminCb = $("#admin-newuser-admin");
    const email = input.value.trim();
    if (!email) return;
    try {
        await api.adminCreateUser(email, adminCb.checked);
        input.value = "";
        adminCb.checked = false;
        await refreshAdmin();
    } catch (err) {
        alert(err.message);
    }
}
$("#admin-newuser-add").addEventListener("click", submitAddUser);
$("#admin-newuser-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitAddUser(); }
});

$("#btn-stop-impersonate").addEventListener("click", async () => {
    try {
        await api.adminStopImpersonate();
        window.location.reload();
    } catch (err) { alert(err.message); }
});

bootstrap();
