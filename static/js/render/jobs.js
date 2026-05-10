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
import { jobs } from "../store.js";

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
    const top = document.createElement("span");
    // Per-row file-path line: rendered for jobs whose payload references
    // a file (extract / embed_text / embed_image / delete_file). Hidden
    // for sync / reindex_folder rows where folder context is shown
    // elsewhere.
    const path = document.createElement("span");
    path.className = "path";
    path.hidden = true;
    const err = document.createElement("span");
    err.className = "err";
    err.hidden = true;
    col.append(top, path, err);

    const tag = document.createElement("span");
    tag.className = "status-tag";

    const retry = document.createElement("button");
    retry.className = "retry";
    retry.textContent = "↻";
    retry.title = "Retry";
    retry.hidden = true;
    retry.addEventListener("click", async () => {
        try { await api.retryJob(jobId); } catch (e) { alert(e.message); }
    });

    li.append(col, tag, retry);
    li._refs = { top, path, err, tag, retry };
    return li;
}

function updateJobRow(li, j) {
    const r = li._refs;
    const top = `${j.kind} #${j.id}`;
    setIfChanged(r.top, "textContent", top);

    // ``display_path`` arrives on the running event AND on the recent-jobs
    // first-load (same name on both wire shapes). Show only when present;
    // otherwise the row is just ``kind #id``.
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
    setIfChanged(r.tag, "textContent", j.state);

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
