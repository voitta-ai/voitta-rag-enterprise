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
    // The top line is split into a primary label and a muted #id chip
    // so the action ("Extract") dominates and the id stays available
    // for cross-referencing logs without shouting on every row.
    const topLabel = document.createElement("span");
    topLabel.className = "label";
    const topId = document.createElement("span");
    topId.className = "job-id";
    top.append(topLabel, topId);
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
    col.append(top, path, err);

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
        try { await api.retryJob(jobId); } catch (e) { alert(e.message); }
    });

    li.append(col, tag, cancel, retry);
    li._refs = { topLabel, topId, path, err, tag, cancel, retry };
    return li;
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
