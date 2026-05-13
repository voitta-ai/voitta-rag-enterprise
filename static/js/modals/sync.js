// Google Drive sync modal + Google Drive folder picker.
//
// One Drive sync source per managed folder root. The modal loads the
// existing config (if any), lets the user edit + save, and offers
// "Sync now" to enqueue a sync job.
//
// Two auth modes:
// - OAuth: client_id + client_secret + Connect → refresh_token. The
//   provider picker fills the credential fields from saved
//   ``/api/admin/auth-providers`` rows; manual entry still works.
// - Service account: paste a key JSON. Folder picker can list folders
//   the SA has been granted access to.
//
// The folder picker submodal is co-located here because it's tightly
// coupled — it reads the in-memory ``gdFolders`` list, writes the
// user's selection back via ``setGdFolders``, and is only opened
// from this file.

import { api } from "../api.js";
import { getSelectedFolderId } from "../flows/selection.js";
import { folders, syncSources } from "../store.js";

const $ = (sel) => document.querySelector(sel);

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
    _snapshotSavedGdFolders([]);
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
    // gdGoogleProviders map. NFS status probe runs in parallel — it
    // just toggles option visibility and doesn't gate the load.
    $("#sync-gd-provider-picker").value = "";
    $("#sync-gd-pane-oauth").classList.remove("provider-picked");
    nfsRefreshStatus();
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
    $("#sync-form-nfs").hidden = t !== "nfs";
    if (t === "google_drive") {
        // Mirror the URL the backend will hand to Google so the user can
        // copy-paste it verbatim into "Authorized redirect URIs".
        const hint = $("#sync-gd-redirect-hint");
        if (hint) hint.textContent = `${window.location.origin}/api/sync/oauth/google/callback`;
    }
    if (t === "nfs") {
        // Refresh on every entry — the admin may have toggled NFS off
        // since the modal opened. nfsRefreshStatus also re-paints the
        // root-display field.
        nfsRefreshStatus().then(() => nfsBrowseTo(nfsCurrentPath));
    }
}

// ---------------------------------------------------------------------------
// NFS picker — server-side, scoped under the admin NFS root
// ---------------------------------------------------------------------------

let nfsCurrentPath = "";   // POSIX relative path; "" = root
let nfsAvailable = false;  // last status probe

async function nfsRefreshStatus() {
    try {
        const s = await api.nfsStatus();
        nfsAvailable = !!s.available;
        const opt = $("#sync-type-option-nfs");
        if (opt) opt.hidden = !s.available;
        const rootDisplay = $("#sync-nfs-root-display");
        if (rootDisplay) {
            rootDisplay.value = s.nfs_root || "";
            rootDisplay.placeholder = s.available
                ? ""
                : s.nfs_root
                    ? `${s.nfs_root} — unavailable (${s.status})`
                    : "(set the NFS root in Admin → Storage)";
        }
        return s;
    } catch (err) {
        nfsAvailable = false;
        const opt = $("#sync-type-option-nfs");
        if (opt) opt.hidden = true;
        return { available: false, status: "error" };
    }
}

async function nfsBrowseTo(rel) {
    nfsCurrentPath = rel || "";
    $("#sync-nfs-subpath").value = nfsCurrentPath;
    const list = $("#sync-nfs-entries");
    const hint = $("#sync-nfs-hint");
    list.innerHTML = "";
    if (!nfsAvailable) {
        hint.textContent = "NFS is unavailable — ask an admin to configure the NFS root.";
        return;
    }
    try {
        const out = await api.nfsBrowse(nfsCurrentPath);
        if (!out.entries.length) {
            const li = document.createElement("li");
            li.className = "muted";
            li.textContent = "(no sub-folders here — this folder will sync as-is)";
            list.append(li);
        }
        for (const e of out.entries) {
            const li = document.createElement("li");
            li.textContent = e.name;
            li.title = e.rel_path;
            li.addEventListener("click", () => nfsBrowseTo(e.rel_path));
            list.append(li);
        }
        hint.textContent = nfsCurrentPath
            ? `Will sync ${nfsCurrentPath}/. Click an entry to descend further.`
            : "Will sync the entire NFS root. Click an entry to scope down.";
    } catch (err) {
        hint.textContent = `Browse failed: ${err.message}`;
    }
}

$("#sync-nfs-up").addEventListener("click", () => {
    if (!nfsCurrentPath) return;
    const parts = nfsCurrentPath.split("/");
    parts.pop();
    nfsBrowseTo(parts.join("/"));
});
$("#sync-nfs-clear").addEventListener("click", () => nfsBrowseTo(""));

// Mirror of the user's current selection in the GD picker; flushed to the
// form on every set. Keeps the rendered list and the form-config call in
// sync without parsing the DOM at submit time.
let gdFolders = [];

// Last-known-saved set of Drive folder IDs. Used by the Save handler
// to detect folder *removals* (the cleanup pass deletes locally-mirrored
// files for any folder that's no longer selected — see
// services/sync/google_drive.py). When the user removes folders we
// prompt before saving so they don't accidentally trash indexed data
// without realizing it. Refreshed every time the modal loads from
// server, every successful Save, and every Sync-now.
let savedGdFolderIds = new Set();

function _snapshotSavedGdFolders(folders) {
    savedGdFolderIds = new Set((folders || []).map((f) => String(f.id || "")));
}

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
        } else if (src.source_type === "nfs" && src.nfs) {
            // Re-probe status so the option visibility is fresh; then
            // populate the saved subpath and walk to it. If the admin
            // disabled NFS since the row was saved, the picker shows
            // an unavailable banner — the user can still see what was
            // configured but cannot trigger a sync.
            await nfsRefreshStatus();
            nfsCurrentPath = src.nfs.subpath || "";
            await nfsBrowseTo(nfsCurrentPath);
        } else if (src.source_type === "google_drive" && src.google_drive) {
            const gd = src.google_drive;
            $("#sync-gd-client-id").value = gd.client_id || "";
            preselectGdProviderByClientId(gd.client_id || "");
            setGdFolders(gd.folders || []);
            _snapshotSavedGdFolders(gd.folders || []);
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

    // Status / last-synced live in one row, with an inline Clear
    // button that wipes the persisted ``sync_error`` once the user
    // has read it. The error block below is fixed-height + scrolls;
    // entries are reversed so the most recent issue is on top
    // (operators care about "what just broke", not "what broke first").
    line.innerHTML = "";

    const top = document.createElement("div");
    top.className = "sync-status-line-top";
    const summary = document.createElement("span");
    const parts = [`status: ${src.sync_status}`];
    if (src.last_synced_at) {
        const d = new Date(src.last_synced_at * 1000);
        parts.push(`last: ${d.toLocaleString()}`);
    }
    summary.textContent = parts.join(" · ");
    top.appendChild(summary);

    if (src.sync_error) {
        const clearBtn = document.createElement("button");
        clearBtn.type = "button";
        clearBtn.className = "btn btn-secondary btn-sm sync-error-clear";
        clearBtn.textContent = "Clear errors";
        clearBtn.title = "Wipe the stored error so the modal renders cleanly on the next open";
        clearBtn.addEventListener("click", async () => {
            clearBtn.disabled = true;
            try {
                const out = await api.clearSyncError(syncFolderId);
                renderSyncStatus(out);
            } catch (err) {
                clearBtn.disabled = false;
                alert(err.message);
            }
        });
        top.appendChild(clearBtn);
    }
    line.appendChild(top);

    if (src.sync_error) {
        const errBlock = document.createElement("pre");
        errBlock.className = "sync-error-block";

        // Reverse: connectors join multiple lines with "\n" / "; " into
        // one ``sync_error`` string, and the most recently-appended one
        // is at the end. Flip so newest reads first. Pure newlines are
        // the canonical separator (preflight uses "\n", connector
        // GoogleDriveSyncStats joins ``stats.errors`` with "; ").
        const lines = src.sync_error.split(/\r?\n/);
        const reversed = lines.reverse().join("\n");

        // Detect URLs in the message and turn them into anchors so the
        // user can click straight into the GCP "Enable API" page.
        // ``<pre>`` preserves newlines / spacing; we rebuild content
        // with link nodes interleaved.
        const urlRe = /(https?:\/\/[^\s)]+)/g;
        let last = 0;
        let m;
        while ((m = urlRe.exec(reversed)) !== null) {
            if (m.index > last) {
                errBlock.appendChild(document.createTextNode(reversed.slice(last, m.index)));
            }
            const a = document.createElement("a");
            a.href = m[1];
            a.target = "_blank";
            a.rel = "noopener";
            a.textContent = m[1];
            errBlock.appendChild(a);
            last = m.index + m[1].length;
        }
        if (last < reversed.length) {
            errBlock.appendChild(document.createTextNode(reversed.slice(last)));
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
    // every source so the backend doesn't need source-specific
    // scheduling code.
    const autoEnabled = $("#sync-auto-enabled").checked;
    const autoHours = Math.max(1, Math.min(24, Number($("#sync-auto-hours").value) || 6));
    const base = { auto_sync_enabled: autoEnabled, auto_sync_hours: autoHours };
    if (t === "github") return { ...base, source_type: "github", github: ghFormConfig() };
    if (t === "google_drive") return { ...base, source_type: "google_drive", google_drive: gdFormConfig() };
    if (t === "nfs") return { ...base, source_type: "nfs", nfs: { subpath: nfsCurrentPath } };
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

// Drive-folder removals trigger orphan-cleanup on the next sync —
// files mirrored locally for any removed folder get deleted from disk,
// then the watcher cascades that into SQLite + Qdrant removals. The
// user almost certainly wants that, but they need to *know* it's
// happening — silent data loss is the worst failure mode. We compare
// the current selection against ``savedGdFolderIds`` (snapshot from
// the server's last state) and warn before saving when anything got
// dropped.
function _removedGdFolderCount() {
    const current = new Set(gdFolders.map((f) => String(f.id || "")));
    let removed = 0;
    for (const id of savedGdFolderIds) {
        if (!current.has(id)) removed += 1;
    }
    return removed;
}

// Persist the form. Re-used by both ``Save`` and ``Sync now`` after
// any confirm prompts have cleared. Returns the server response so
// callers can chain.
async function _doSave() {
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
        _snapshotSavedGdFolders(gd.folders || []);
        setGdConnState({ connected: gd.connected, hasClientSecret: gd.has_client_secret });
    }
    return out;
}

$("#sync-save").addEventListener("click", async () => {
    try {
        // If the user removed Drive folders since the last load/save,
        // make sure they understand the cleanup that's coming on the
        // next sync. "Save & sync now" is the obvious follow-through;
        // "Save only" lets them stage the change for later.
        const removed = _removedGdFolderCount();
        let triggerAfter = false;
        if (removed > 0) {
            const msg =
                `You removed ${removed} Drive folder${removed === 1 ? "" : "s"} ` +
                `from this sync.\n\n` +
                `On the next sync, files mirrored from the removed folder(s) ` +
                `will be deleted from disk, then their SQLite rows and Qdrant ` +
                `points will be wiped.\n\n` +
                `Save and trigger that sync NOW?\n\n` +
                `• OK = Save & Sync now (cleanup runs immediately)\n` +
                `• Cancel = Save only (cleanup waits for your next Sync-now ` +
                `or the auto-sync timer)`;
            triggerAfter = confirm(msg);
        }
        await _doSave();
        if (triggerAfter) {
            await api.triggerSync(syncFolderId);
            alert("Sync queued. Watch the Recent jobs panel for progress; " +
                  "deletions will follow as the watcher catches up to the " +
                  "removed files.");
            closeSyncModal();
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
        await _doSave();
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

// Live status updates while the modal is open.
//
// Without this, the modal renders a snapshot taken at open time: a sync
// job that completes (or errors out, or has its error cleared from
// another tab) while the user is looking at the form leaves the status
// line stuck on the stale value until close+reopen. The backend emits
// folder.sync_source_changed at every state transition; mirror it into
// the status line so the user sees "syncing → idle" / "→ error" live.
//
// Only the status row at the top is touched — the form inputs the user
// may be editing are left alone. We pass the cached form fields (auto-
// sync, etc.) from the last loadSyncSource through so renderSyncStatus
// sees a complete-enough shape, even though it only consults
// sync_status / sync_error / last_synced_at.
syncSources.subscribe((map) => {
    if (syncFolderId == null) return;
    const entry = map.get(syncFolderId);
    if (!entry) return;
    renderSyncStatus({
        sync_status: entry.sync_status,
        sync_error: entry.sync_error,
        last_synced_at: entry.last_synced_at,
    });
});
