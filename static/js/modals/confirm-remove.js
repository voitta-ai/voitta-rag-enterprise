// Danger-confirm modal — the one gate every delete path goes through
// (toolbar Remove for files / subdirs / whole folders, and the row-level
// ✕ buttons in the tree). Replaces the native confirm() dialogs: the
// target is named explicitly, Cancel holds focus so a stray Enter never
// destroys anything, and the destructive button is the only red thing.

const $ = (sel) => document.querySelector(sel);

let _resolve = null;

function settle(answer) {
    $("#remove-backdrop").hidden = true;
    const r = _resolve;
    _resolve = null;
    if (r) r(answer);
}

// Resolves true when the user confirms, false otherwise. A second call
// while open settles the first as cancelled (defensive — shouldn't happen).
export function confirmRemoval({ title, body, detail }) {
    if (_resolve) settle(false);
    $("#remove-title").textContent = title;
    $("#remove-body").textContent = body;
    $("#remove-detail").textContent = detail || "This cannot be undone.";
    $("#remove-backdrop").hidden = false;
    $("#remove-cancel").focus();
    return new Promise((resolve) => { _resolve = resolve; });
}

$("#remove-confirm").addEventListener("click", () => settle(true));
$("#remove-cancel").addEventListener("click", () => settle(false));
$("#remove-close").addEventListener("click", () => settle(false));
$("#remove-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "remove-backdrop") settle(false);
});
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _resolve) settle(false);
});
