// Sidebar tab strip (Details vs Jobs) and the small activity dot on
// the Jobs tab.
//
// Tabs are pure-DOM toggles — no re-render is triggered when the user
// flips between them. A queued WS update lands the moment they flip
// back to the affected tab because the underlying renders are
// state-driven (sidebar / jobs renderers run from store events
// regardless of which tab is visible).

import { jobs } from "../store.js";

const $ = (sel) => document.querySelector(sel);

let activeSidebarTab = "details"; // "details" | "jobs"

export function setSidebarTab(name) {
    if (name !== "details" && name !== "jobs") name = "details";
    if (activeSidebarTab === name) return;
    activeSidebarTab = name;

    const detailsBtn = $("#tab-btn-details");
    const jobsBtn = $("#tab-btn-jobs");
    const detailsPane = $("#tab-pane-details");
    const jobsPane = $("#tab-pane-jobs");

    detailsBtn.classList.toggle("active", name === "details");
    jobsBtn.classList.toggle("active", name === "jobs");
    detailsBtn.setAttribute("aria-selected", String(name === "details"));
    jobsBtn.setAttribute("aria-selected", String(name === "jobs"));
    detailsPane.hidden = name !== "details";
    jobsPane.hidden = name !== "jobs";
}

// Tiny activity indicator on the Jobs tab so the user notices when work
// arrives while they're looking at Details. Red dot if anything errored,
// accent dot if anything queued/running, hidden otherwise.
export function updateJobsTabIndicator() {
    const dot = $("#tab-jobs-dot");
    const btn = $("#tab-btn-jobs");
    if (!dot || !btn) return;
    let hasActive = false;
    let hasError = false;
    for (const j of jobs.get()) {
        if (j.state === "error") { hasError = true; }
        else if (j.state === "queued" || j.state === "running") { hasActive = true; }
    }
    dot.hidden = !(hasActive || hasError);
    if (hasError) {
        btn.dataset.state = "error";
    } else {
        delete btn.dataset.state;
    }
}

// Wire button click handlers exactly once at module load. Safe at top
// level because the modal HTML is in the document by the time any
// module runs (we're imported from main, which is loaded as the page's
// entry point after parsing).
$("#tab-btn-details").addEventListener("click", () => setSidebarTab("details"));
$("#tab-btn-jobs").addEventListener("click", () => setSidebarTab("jobs"));
