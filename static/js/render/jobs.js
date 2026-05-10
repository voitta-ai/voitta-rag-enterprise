// Recent-jobs list (Jobs tab in the sidebar).
//
// Same keyed-reconciliation idea as the folder list: keep one <li> per
// job id alive across renders so a hovered/clicked retry button doesn't
// vanish under the cursor when a fresh job event arrives.

import { api } from "../api.js";
import { reconcileChildren, setIfChanged } from "../dom/reconcile.js";
import { jobs } from "../store.js";

const $ = (sel) => document.querySelector(sel);
const jobRowCache = new Map();

function buildJobRow(jobId) {
    const li = document.createElement("li");
    li.dataset.jobId = String(jobId);

    const col = document.createElement("div");
    col.className = "col";
    const top = document.createElement("span");
    const err = document.createElement("span");
    err.className = "err";
    err.hidden = true;
    col.append(top, err);

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
    li._refs = { top, err, tag, retry };
    return li;
}

function updateJobRow(li, j) {
    const r = li._refs;
    const top = `${j.kind} #${j.id}`;
    setIfChanged(r.top, "textContent", top);

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
    const visible = jobs.get().slice(0, 30);
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
