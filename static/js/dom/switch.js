// iOS-style toggle switch — checkbox + visual track styled in CSS.
//
// Wrapped in a <label> so screen readers and keyboard navigation work
// without extra ARIA. The label intercepts clicks (stopPropagation) so
// flipping the switch never bubbles to a parent row's click handler —
// otherwise toggling a per-folder switch would also select the row.

export function buildSwitch({ checked, disabled, title, onChange }) {
    const wrap = document.createElement("label");
    wrap.className = "folder-switch";
    wrap.title = title || "";
    wrap.addEventListener("click", (e) => e.stopPropagation());

    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!checked;
    input.disabled = !!disabled;
    input.addEventListener("change", () => onChange(input.checked));

    const track = document.createElement("span");
    track.className = "track";

    wrap.append(input, track);
    return wrap;
}
