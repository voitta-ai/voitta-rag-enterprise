// A floating window: moved by its header, resized from any edge or corner,
// minimised to its header. Its rectangle is remembered per browser and kept
// inside the viewport (also after the viewport shrinks).
//
// Markup contract: the element holds a ``[data-drag-handle]`` header, a
// ``[data-minimise]`` button and ``[data-grip="n|s|e|w|ne|nw|se|sw"]``
// resize grips.

const RECT_KEY = "voitta.assistant.rect";
const MIN_W = 360;
const MIN_H = 320;
const MARGIN = 8;
// How much of a window must stay on screen so it can always be grabbed.
const KEEP_VISIBLE = 120;

function defaultRect() {
    const width = Math.min(480, window.innerWidth - 2 * MARGIN);
    const height = Math.min(Math.max(MIN_H, window.innerHeight - 160), 760);
    return {
        left: window.innerWidth - width - 24,
        top: window.innerHeight - height - 88,
        width,
        height,
    };
}

function loadRect() {
    try {
        const rect = JSON.parse(localStorage.getItem(RECT_KEY));
        if (rect && ["left", "top", "width", "height"].every((k) => Number.isFinite(rect[k]))) {
            return rect;
        }
    } catch {
        /* storage unavailable or corrupt: fall back to the default */
    }
    return defaultRect();
}

function saveRect(rect) {
    try {
        localStorage.setItem(RECT_KEY, JSON.stringify(rect));
    } catch {
        /* private mode / blocked storage: the window just won't remember */
    }
}

export function createWindow(el, { onVisibility } = {}) {
    let rect = loadRect();
    let minimised = false;
    const head = el.querySelector("[data-drag-handle]");
    const minimiseButton = el.querySelector("[data-minimise]");

    function clamp() {
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        rect.width = Math.min(Math.max(rect.width, MIN_W), vw - 2 * MARGIN);
        rect.height = Math.min(Math.max(rect.height, MIN_H), vh - 2 * MARGIN);
        rect.left = Math.min(Math.max(rect.left, MARGIN - rect.width + KEEP_VISIBLE), vw - KEEP_VISIBLE);
        rect.top = Math.min(Math.max(rect.top, MARGIN), vh - 46);
    }

    function apply() {
        clamp();
        el.style.left = `${rect.left}px`;
        el.style.top = `${rect.top}px`;
        el.style.width = `${rect.width}px`;
        el.style.height = minimised ? "auto" : `${rect.height}px`;
    }

    // Pointer-captured drag: keeps receiving moves when the cursor leaves the
    // window or crosses an iframe, and always ends with a pointerup.
    function track(event, onMove) {
        event.preventDefault();
        const target = event.currentTarget;
        target.setPointerCapture(event.pointerId);
        const start = { x: event.clientX, y: event.clientY, rect: { ...rect } };
        el.classList.add("is-dragging");
        const move = (e) => {
            onMove(e.clientX - start.x, e.clientY - start.y, start.rect);
            apply();
        };
        const end = () => {
            el.classList.remove("is-dragging");
            target.removeEventListener("pointermove", move);
            target.removeEventListener("pointerup", end);
            target.removeEventListener("pointercancel", end);
            saveRect(rect);
        };
        target.addEventListener("pointermove", move);
        target.addEventListener("pointerup", end);
        target.addEventListener("pointercancel", end);
    }

    const isControl = (t) => t.closest("button, select, input, textarea, label, a");

    head.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || isControl(event.target)) return;
        track(event, (dx, dy, from) => {
            rect.left = from.left + dx;
            rect.top = from.top + dy;
        });
    });
    head.addEventListener("dblclick", (event) => {
        if (!isControl(event.target)) setMinimised(!minimised);
    });

    for (const grip of el.querySelectorAll("[data-grip]")) {
        const edge = grip.dataset.grip;
        grip.addEventListener("pointerdown", (event) => {
            if (event.button !== 0) return;
            track(event, (dx, dy, from) => {
                if (edge.includes("e")) rect.width = Math.max(MIN_W, from.width + dx);
                if (edge.includes("s")) rect.height = Math.max(MIN_H, from.height + dy);
                if (edge.includes("w")) {
                    rect.width = Math.max(MIN_W, from.width - dx);
                    rect.left = from.left + from.width - rect.width;
                }
                if (edge.includes("n")) {
                    rect.height = Math.max(MIN_H, from.height - dy);
                    rect.top = from.top + from.height - rect.height;
                }
            });
        });
    }

    function setMinimised(value) {
        minimised = value;
        el.classList.toggle("is-minimised", minimised);
        minimiseButton.setAttribute("aria-expanded", String(!minimised));
        minimiseButton.textContent = minimised ? "□" : "–";
        minimiseButton.setAttribute("aria-label", minimised ? "Restore" : "Minimise");
        minimiseButton.title = minimised ? "Restore" : "Minimise";
        apply();
        onVisibility?.();
    }

    minimiseButton.addEventListener("click", () => setMinimised(!minimised));
    window.addEventListener("resize", () => { if (!el.hidden) apply(); });

    return {
        get isOpen() { return !el.hidden; },
        get isAttentive() { return !el.hidden && !minimised; },
        open() {
            el.hidden = false;
            apply();
            onVisibility?.();
        },
        close() {
            el.hidden = true;
            onVisibility?.();
        },
        restore() {
            if (minimised) setMinimised(false);
        },
    };
}
