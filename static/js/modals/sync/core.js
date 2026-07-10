// ----- Sync modal — source-agnostic core -----
//
// Shape: every managed folder root has zero or one sync source. The modal
// loads the existing config (if any), lets the user edit + save, and offers
// "Sync now" to enqueue a sync job. Credentials in fields stay in the form
// until Save (they aren't echoed back from the server — only has_pat /
// has_ssh_key style flags are returned, used to gray out the inputs).
//
// Everything source-specific lives in the connector modules (github.js,
// google_drive.js, …), which register a handler per source_type in
// registry.js; the dispatchers below loop over that table instead of
// if/elif chains. Dependency rule: core never imports a connector —
// connectors import core (loadSyncSource, syncBody, _doSave, applyChrome,
// closeSyncModal).

import { api } from "../../api.js";
import { getSelectedFolderId } from "../../flows/selection.js";
import { folders, syncConfigs, syncSources } from "../../store.js";
import { SOURCES } from "./registry.js";
import { $, ctx } from "./state.js";

function openSyncModal() {
    if (!getSelectedFolderId()) return;
    ctx.folderId = getSelectedFolderId();
    const folder = folders.get().find((f) => f.id === ctx.folderId);
    if (!folder) return;

    $("#sync-title").textContent = `Configure sync — ${folder.display_name}`;
    $("#sync-backdrop").hidden = false;
    $("#sync-status-line").hidden = true;
    $("#sync-delete").hidden = true;

    // Reset to defaults; loadSyncSource() will fill from server if present.
    // Each connector's reset() restores its own inputs (and kicks its
    // availability probes — NFS status, "This Mac" tab visibility, MS
    // provider pickers — which run in parallel: they just toggle option
    // visibility and don't gate the load).
    $("#sync-type").value = "github";
    setSyncType("github");
    for (const h of SOURCES.values()) h.reset?.();

    // Auto-sync defaults: off, 6h. loadSyncSource overrides from the row.
    $("#sync-auto-enabled").checked = false;
    $("#sync-auto-hours").value = "6";
    $("#sync-auto-hours").disabled = true;

    // Refresh + load in sequence: google_drive's beforeLoad() repopulates
    // its provider picker so the pre-select-by-client_id pass in
    // loadSyncSource sees a populated provider map; its afterLoad() then
    // auto-picks the single-provider case without overriding an existing
    // sync's saved value.
    const gates = [...SOURCES.values()].map((h) => h.beforeLoad?.()).filter(Boolean);
    Promise.allSettled(gates)
        .finally(loadSyncSource)
        .finally(() => {
            for (const h of SOURCES.values()) h.afterLoad?.();
        });
}

export function setSyncType(t) {
    for (const h of SOURCES.values()) {
        if (h.paneId) $(h.paneId).hidden = h.tab !== t;
    }
    for (const h of SOURCES.values()) {
        if (h.tab === t) h.onShow?.();
    }
    // Reconcile the Save/Sync-now footer + shared GD selector with the active
    // Google Drive sub-tab (the local "This Mac" tab hides both).
    applyChrome();
}

// The local "This Mac" Google Drive tab CREATES a new indexed folder via its
// own "Connect & index" button, so the shared Drive-folder selector and the
// standard Save / Sync-now / Remove footer + auto-sync row don't apply to it.
// A handler opts in via hidesChrome() — google_drive_local returns true iff
// source == google_drive AND its sub-tab == local.
export function applyChrome() {
    const local = [...SOURCES.values()].some((h) => h.hidesChrome?.() === true);
    const shared = $("#sync-gd-shared");
    if (shared) shared.hidden = local;
    $("#sync-save").hidden = local;
    $("#sync-trigger").hidden = local;
    if (local) $("#sync-delete").hidden = true;
    const autoRow = document.querySelector(".sync-auto-row");
    if (autoRow) autoRow.hidden = local;
}

export function closeSyncModal() {
    $("#sync-backdrop").hidden = true;
    ctx.folderId = null;
}

export async function loadSyncSource() {
    try {
        // Read the config from the live store first. It's kept fresh by the
        // ``folder.sync_config_changed`` WS push (on save, in this or any tab),
        // so reopening after a change needs no refetch. Lazy-load + seed only
        // when this folder's config isn't cached yet — the heavy per-folder
        // connector blob is intentionally not in the global snapshot.
        const cache = syncConfigs.get();
        let src;
        if (cache.has(ctx.folderId)) {
            src = cache.get(ctx.folderId);  // may be null (config deleted)
        } else {
            src = await api.getSync(ctx.folderId);
            syncConfigs.update((m) => {
                const next = new Map(m);
                next.set(ctx.folderId, src || null);
                return next;
            });
        }
        if (!src) return;
        // google_drive_local isn't a Source dropdown entry — it's the "This Mac"
        // tab under Google Drive. ``handler.tab`` maps it so reopening such a
        // folder's dialog lands on that tab instead of an empty form.
        const handler = SOURCES.get(src.source_type);
        const t = handler?.tab || src.source_type;
        $("#sync-type").value = t;
        setSyncType(t);
        // Per-source form population. A ``true`` return means the handler
        // rendered its own status and has no shared footer
        // (google_drive_local) — skip the common tail below.
        if ((await handler?.load?.(src)) === true) return;
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
                const out = await api.clearSyncError(ctx.folderId);
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

export function syncBody() {
    const t = $("#sync-type").value;
    // Auto-sync settings live alongside source_type — same payload for
    // every source so the backend doesn't need source-specific
    // scheduling code.
    const autoEnabled = $("#sync-auto-enabled").checked;
    const autoHours = Math.max(1, Math.min(24, Number($("#sync-auto-hours").value) || 6));
    const base = { auto_sync_enabled: autoEnabled, auto_sync_hours: autoHours };
    const handler = SOURCES.get(t);
    if (!handler?.formConfig) throw new Error(`Unknown source_type: ${t}`);
    return { ...base, source_type: t, [t]: handler.formConfig() };
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

$("#sync-close").addEventListener("click", closeSyncModal);
$("#sync-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "sync-backdrop") closeSyncModal();
});

$("#btn-sync").addEventListener("click", openSyncModal);

// Persist the form. Re-used by both ``Save`` and ``Sync now`` after
// any confirm prompts have cleared. Returns the server response so
// callers can chain. The per-source post-save refresh (wipe typed-in
// secrets, flip placeholders to "(saved — type to replace)", scope
// re-checks, …) lives on each handler's afterSave().
export async function _doSave() {
    const out = await api.putSync(ctx.folderId, syncBody());
    $("#sync-delete").hidden = false;
    renderSyncStatus(out);
    SOURCES.get(out.source_type)?.afterSave?.(out);
    return out;
}

$("#sync-save").addEventListener("click", async () => {
    try {
        // A handler's beforeSaveConfirm() may prompt about destructive
        // side effects (google_drive: removed folders get cleaned up on
        // the next sync) — it returns true when the user chose
        // "Save & Sync now" so the cleanup runs immediately.
        let triggerAfter = false;
        for (const h of SOURCES.values()) {
            if (h.beforeSaveConfirm?.() === true) triggerAfter = true;
        }
        await _doSave();
        if (triggerAfter) {
            await api.triggerSync(ctx.folderId);
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
        await api.triggerSync(ctx.folderId);
        alert("Sync queued. Watch the Recent jobs panel for progress.");
        closeSyncModal();
    } catch (err) {
        alert(err.message);
    }
});

$("#sync-delete").addEventListener("click", async () => {
    if (!confirm("Remove the sync configuration?\n\nFiles already on disk will not be deleted.")) return;
    try {
        await api.deleteSync(ctx.folderId);
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
    if (ctx.folderId == null) return;
    const entry = map.get(ctx.folderId);
    if (!entry) return;
    renderSyncStatus({
        sync_status: entry.sync_status,
        sync_error: entry.sync_error,
        last_synced_at: entry.last_synced_at,
    });
});
