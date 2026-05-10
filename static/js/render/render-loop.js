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

export function scheduleFullRender() {
    if (fullRenderPending) return;
    fullRenderPending = true;
    requestAnimationFrame(() => {
        fullRenderPending = false;
        sidebarRenderPending = false; // a full render covers the sidebar too
        if (_renderers.full) _renderers.full();
    });
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
