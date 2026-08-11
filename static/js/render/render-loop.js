// rAF-coalesced render scheduler.
//
// Store subscribers fire synchronously on every WS event. Under heavy
// indexing that's hundreds per second — each one used to tear down and
// rebuild the entire tree, starving input handling and racing with
// in-flight clicks (mousedown lands on a node that gets destroyed before
// mouseup). We coalesce to one render per animation frame: subscribers
// just flip a dirty flag; the rAF callback does the actual DOM work.
//
// Browser event-loop ordering (input → microtasks → rAF → paint) means a
// click runs to completion before the next render fires, so the target
// node is guaranteed alive while the handler runs.
//
// Architecture: this is a leaf module — it imports nothing from other
// app code. Renderers are registered once at boot via :func:`setRenderers`,
// keeping every other module free of circular imports while still
// letting them call schedulers from event handlers.

let fullRenderPending = false;
let sidebarRenderPending = false;
let jobsRenderPending = false;

const _renderers = {
    full: null,
    sidebar: null,
    jobs: null,
};

export function setRenderers(renderers) {
    if (renderers.full) _renderers.full = renderers.full;
    if (renderers.sidebar) _renderers.sidebar = renderers.sidebar;
    if (renderers.jobs) _renderers.jobs = renderers.jobs;
}

// Upper bound on full-tree renders during sustained delta bursts. rAF alone
// caps us at ~60/s, but a full tree render over thousands of rows is expensive
// enough that 60/s still pins the main thread through a long clone/sync. A
// trailing throttle drops that to ~5/s during a burst while keeping the FIRST
// render after idle on the very next frame — interactive updates stay snappy
// because the throttle only engages when renders are already back-to-back.
const FULL_RENDER_MIN_INTERVAL_MS = 200;
let lastFullRenderTs = 0;
let fullRenderTrailingTimer = null;

function _now() {
    return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function _runFullRender() {
    fullRenderPending = false;
    sidebarRenderPending = false; // a full render covers the sidebar too
    lastFullRenderTs = _now();
    if (_renderers.full) _renderers.full();
}

export function scheduleFullRender() {
    if (fullRenderPending) return;
    const sinceLast = _now() - lastFullRenderTs;
    if (sinceLast >= FULL_RENDER_MIN_INTERVAL_MS) {
        // Idle-ish: render on the next animation frame (snappy).
        if (fullRenderTrailingTimer !== null) {
            clearTimeout(fullRenderTrailingTimer);
            fullRenderTrailingTimer = null;
        }
        fullRenderPending = true;
        requestAnimationFrame(_runFullRender);
    } else if (fullRenderTrailingTimer === null) {
        // Mid-burst: coalesce every request in this window into ONE trailing
        // render at the window edge.
        fullRenderTrailingTimer = setTimeout(() => {
            fullRenderTrailingTimer = null;
            if (fullRenderPending) return;
            fullRenderPending = true;
            requestAnimationFrame(_runFullRender);
        }, FULL_RENDER_MIN_INTERVAL_MS - sinceLast);
    }
}

export function scheduleSidebarRender() {
    if (sidebarRenderPending || fullRenderPending) return;
    sidebarRenderPending = true;
    requestAnimationFrame(() => {
        sidebarRenderPending = false;
        if (_renderers.sidebar) _renderers.sidebar();
    });
}

export function scheduleJobsRender() {
    if (jobsRenderPending) return;
    jobsRenderPending = true;
    requestAnimationFrame(() => {
        jobsRenderPending = false;
        if (_renderers.jobs) _renderers.jobs();
    });
}
