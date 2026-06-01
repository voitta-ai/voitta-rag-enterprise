// Recent-jobs list (Jobs tab in the sidebar).
//
// Same keyed-reconciliation idea as the folder list: keep one <li> per
// job id alive across renders so a hovered/clicked retry button doesn't
// vanish under the cursor when a fresh job event arrives.
//
// Ordering: running first, then queued, then everything else (done /
// error). Within each bucket, newest id first. Without this, a queue
// of 800 fresh extracts would push the actually-running job (older id)
// off the visible 30-row window — see the "no running jobs in the
// view" bug report. The same pinning happens server-side in
// /api/jobs/recent so the first-load snapshot includes the running
// job too; this client-side sort handles subsequent WS updates.

import { api } from "../api.js";
import { reconcileChildren, setIfChanged } from "../dom/reconcile.js";
import { jobs, reindexProgress } from "../store.js";

const $ = (sel) => document.querySelector(sel);
const jobRowCache = new Map();

const _STATE_RANK = { running: 0, queued: 1 };

function jobSortRank(j) {
    // Anything not running / queued sorts third (done, error, …); the
    // ``?? 2`` keeps ranks stable across unknown future states.
    return _STATE_RANK[j.state] ?? 2;
}

function buildJobRow(jobId) {
    const li = document.createElement("li");
    li.dataset.jobId = String(jobId);

    const col = document.createElement("div");
    col.className = "col";
    // Lines inside the col are <div>s rather than <span>s on purpose:
    // a flex-column of block children copies as one-line-per-child in
    // every browser, so selecting a row no longer pastes as
    // "Extract  #17945branches/main/...queued".
    const top = document.createElement("div");
    top.className = "top";
    // Disclosure chevron — shown only for rows that have detail to expand
    // (a result summary, or an error). Toggles the detail panel below.
    const expand = document.createElement("span");
    expand.className = "job-expand";
    expand.textContent = "▸";
    expand.hidden = true;
    expand.style.cursor = "pointer";
    expand.style.userSelect = "none";
    expand.title = "Show details";
    // The top line is split into a primary label and a muted #id chip
    // so the action ("Extract") dominates and the id stays available
    // for cross-referencing logs without shouting on every row.
    const topLabel = document.createElement("span");
    topLabel.className = "label";
    const topId = document.createElement("span");
    topId.className = "job-id";
    top.append(expand, topLabel, topId);
    // Per-row file-path line: rendered for jobs whose payload references
    // a file (extract / embed_text / embed_image / delete_file). Hidden
    // for sync / reindex_folder rows where folder context is shown
    // elsewhere.
    const path = document.createElement("div");
    path.className = "path";
    path.hidden = true;
    const err = document.createElement("div");
    err.className = "err";
    err.hidden = true;
    // Expandable detail panel: key/value summary of the job's result
    // (sync stats, etc.) plus any per-item errors. Collapsed by default.
    const detail = document.createElement("div");
    detail.className = "job-detail";
    detail.hidden = true;
    col.append(top, path, err, detail);

    // Toggle expand/collapse; state lives on the <li> so it survives the
    // render loop's row reuse (rows are cached by job id).
    const toggle = () => {
        const open = li.dataset.expanded === "1";
        li.dataset.expanded = open ? "0" : "1";
        expand.textContent = open ? "▸" : "▾";
        detail.hidden = open;
    };
    expand.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });

    const tag = document.createElement("span");
    tag.className = "status-tag";

    const cancel = document.createElement("button");
    cancel.className = "cancel";
    cancel.textContent = "✕";
    cancel.title = "Cancel this job";
    cancel.hidden = true;
    cancel.addEventListener("click", async () => {
        if (!confirm(
            "Cancel this job?\n\n" +
            "Queued jobs are dropped immediately. A running embed/sync " +
            "is marked cancelled in the UI but the worker thread keeps " +
            "running silently in the background until its current pass " +
            "completes (Python has no clean interrupt for that).\n\n" +
            "PDF parses are killed at the subprocess level."
        )) return;
        cancel.disabled = true;
        try {
            const out = await api.cancelJob(jobId);
            if (out.note) alert(out.note);
        } catch (e) {
            cancel.disabled = false;
            alert(e.message);
        }
    });

    const retry = document.createElement("button");
    retry.className = "retry";
    retry.textContent = "↻";
    retry.title = "Retry";
    retry.hidden = true;
    retry.addEventListener("click", async () => {
        // Optimistic: disable so a double-click can't enqueue two retries.
        // The row reconciles from the WS job.* events; on error we re-enable.
        retry.disabled = true;
        try {
            await api.retryJob(jobId);
        } catch (e) {
            retry.disabled = false;
            alert(e.message);
        }
    });

    li.append(col, tag, cancel, retry);
    li._refs = { expand, topLabel, topId, path, err, detail, tag, cancel, retry };
    return li;
}

// Pretty labels for known result keys; unknown keys fall back to a
// humanised form of the raw key so a new connector stat still renders.
const RESULT_LABEL = {
    files_added: "Files added",
    files_updated: "Files updated",
    files_removed: "Files removed",
    files_skipped: "Files skipped",
    files_404: "Files missing (404)",
    pages_written: "Pages written",
    notes_written: "Notes written",
    tabs_written: "Tabs written",
    sites_synced: "Sites synced",
    branches_synced: "Branches synced",
    commits_written: "Commits written",
    branches_removed: "Branches removed",
    elapsed_s: "Elapsed (s)",
};

function _humanKey(k) {
    return RESULT_LABEL[k] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

// Render a job's result dict into the detail panel: scalar stats as a compact
// key/value grid, and any ``errors`` array as a highlighted list. Returns true
// if it produced any content.
function renderJobDetail(detailEl, result) {
    detailEl.textContent = "";
    if (!result || typeof result !== "object") return false;

    const grid = document.createElement("div");
    grid.className = "job-detail-grid";
    let scalarCount = 0;
    for (const [k, v] of Object.entries(result)) {
        if (k === "errors" || v === null || typeof v === "object") continue;
        const row = document.createElement("div");
        row.className = "job-detail-row";
        const key = document.createElement("span");
        key.className = "k";
        key.textContent = _humanKey(k);
        const val = document.createElement("span");
        val.className = "v";
        val.textContent = String(v);
        // De-emphasise zero counts so the eye lands on what actually happened.
        if (v === 0) row.style.opacity = "0.5";
        row.append(key, val);
        grid.append(row);
        scalarCount++;
    }
    if (scalarCount) detailEl.append(grid);

    const errors = Array.isArray(result.errors) ? result.errors : [];
    if (errors.length) {
        const box = document.createElement("div");
        box.className = "job-detail-errors";
        const head = document.createElement("div");
        head.className = "job-detail-errors-head";
        head.textContent = `${errors.length} error${errors.length > 1 ? "s" : ""}`;
        box.append(head);
        for (const e of errors) {
            const line = document.createElement("div");
            line.className = "job-detail-error";
            line.textContent = typeof e === "string" ? e : JSON.stringify(e);
            box.append(line);
        }
        detailEl.append(box);
    }
    return scalarCount > 0 || errors.length > 0;
}

// Compact wall-clock duration: 12s / 3m 04s / 1h 12m. Sized so it fits
// inside the status pill without making the row jump as the value grows.
function formatElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

// Human verbs for the kind enum. Falls back to the raw kind for any
// future kind we haven't taught the SPA about, so a new worker type
// still renders something instead of crashing the row.
const KIND_LABEL = {
    extract:        "Extract",
    embed_text:     "Embed text",
    embed_image:    "Embed image",
    delete_file:    "Delete",
    sync:           "Sync",
    reindex_folder: "Reindex folder",
};

function updateJobRow(li, j) {
    const r = li._refs;
    let labelText = KIND_LABEL[j.kind] || j.kind;
    // Surface the active reindex phase inline on running reindex_folder
    // rows so the label reads "Reindex folder — wiping 800/1613" instead
    // of just "Reindex folder". boot.js calls scheduleJobsRender on every
    // reindexProgress tick so this string refreshes as the worker advances.
    // ``phase === 'done'`` clears the entry from the store, so we never see it.
    if (j.state === "running" && j.kind === "reindex_folder") {
        const fid = j.payload && j.payload.folder_id;
        if (fid != null) {
            const prog = reindexProgress.get().get(fid);
            if (prog && prog.phase) {
                const done = prog.done ?? 0;
                const total = prog.total ?? 0;
                labelText += total
                    ? ` — ${prog.phase} ${done}/${total}`
                    : ` — ${prog.phase}`;
            }
        }
    }
    setIfChanged(r.topLabel, "textContent", labelText);
    setIfChanged(r.topId, "textContent", `#${j.id}`);

    // ``display_path`` arrives on the running event AND on the recent-jobs
    // first-load (same name on both wire shapes). Show only when present;
    // otherwise the row is just ``label #id``.
    if (j.display_path) {
        setIfChanged(r.path, "textContent", j.display_path);
        if (r.path.hidden) r.path.hidden = false;
    } else if (!r.path.hidden) {
        r.path.hidden = true;
    }

    if (j.state === "error" && j.error) {
        const errText = j.error.length > 200 ? j.error.slice(0, 200) + "…" : j.error;
        setIfChanged(r.err, "textContent", errText);
        if (r.err.hidden) r.err.hidden = false;
    } else if (!r.err.hidden) {
        r.err.hidden = true;
    }

    setIfChanged(r.tag, "className", `status-tag ${j.state}`);
    // For running rows, append a live elapsed-time tail ("running 12s") so
    // the row reports forward motion even between server events. ws.js
    // stamps ``started_at_ms`` on job.started; if it's missing (e.g. the row
    // arrived via /api/jobs/recent without a client stamp) we render just
    // the status word — the tail will start ticking on the next event.
    let statusText = j.state;
    if (j.state === "running" && j.started_at_ms) {
        statusText += " " + formatElapsed(Date.now() - j.started_at_ms);
    }
    setIfChanged(r.tag, "textContent", statusText);

    // Cancel button: visible while the job can still be aborted. Done /
    // error rows hide it (terminal). Reset disabled when the row
    // re-enters a cancellable state — happens after the retry path.
    const showCancel = j.state === "queued" || j.state === "running";
    if (r.cancel.hidden === showCancel) r.cancel.hidden = !showCancel;
    if (showCancel && r.cancel.disabled) r.cancel.disabled = false;

    const showRetry = j.state === "error";
    if (r.retry.hidden === showRetry) r.retry.hidden = !showRetry;

    // Expandable detail: present when the handler returned a result summary
    // (sync stats, etc.). Render into the (hidden) panel and reveal the
    // chevron; the panel itself follows the row's persisted expanded state.
    const hasDetail = renderJobDetail(r.detail, j.result);
    if (r.expand.hidden === hasDetail) r.expand.hidden = !hasDetail;
    if (!hasDetail) {
        li.dataset.expanded = "0";
        r.expand.textContent = "▸";
        if (!r.detail.hidden) r.detail.hidden = true;
    } else {
        const open = li.dataset.expanded === "1";
        r.expand.textContent = open ? "▾" : "▸";
        if (r.detail.hidden === open) r.detail.hidden = !open;
    }
}

export function renderJobs() {
    const ul = $("#jobs");
    // Stable sort: running first, then queued, then done/error. Within
    // each bucket, newest id first — that's what the user wants when
    // scanning a long list.
    const sorted = [...jobs.get()].sort((a, b) => {
        const dr = jobSortRank(a) - jobSortRank(b);
        if (dr !== 0) return dr;
        return b.id - a.id;
    });
    const visible = sorted.slice(0, 30);
    const targetRows = [];
    const seenKeys = new Set();

    for (const j of visible) {
        const key = `job:${j.id}`;
        let li = jobRowCache.get(key);
        if (!li) {
            li = buildJobRow(j.id);
            jobRowCache.set(key, li);
        }
        updateJobRow(li, j);
        targetRows.push(li);
        seenKeys.add(key);
    }

    reconcileChildren(ul, targetRows, seenKeys, jobRowCache);
}
