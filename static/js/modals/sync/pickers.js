// ---------------------------------------------------------------------------
// Shared list picker (Jira projects / SharePoint sites / Teams user)
//
// Built to stay responsive on enterprise-scale lists (thousands of rows). The
// naive "render every row with a per-row listener, rebuild on every keystroke"
// approach froze the whole tab on big Jira orgs. This helper instead:
//   • renders at most PICKER_RENDER_CAP rows via a single innerHTML write,
//   • uses ONE delegated listener for the whole list (not one per checkbox),
//   • debounces filtering, and
//   • supports an async ``search`` source so huge orgs are queried server-side
//     instead of downloaded in full.
// ---------------------------------------------------------------------------

const PICKER_RENDER_CAP = 200;   // max rows painted at once (client-side)
const PICKER_SEARCH_LIMIT = 100; // matches the server-side search page size

export function escHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
}

// Split a filter string into a trimmed, non-empty token list. Separators are
// commas and newlines only (NOT spaces) so multi-word names survive. Two or
// more tokens switches the picker into exact-match mode.
export function parsePickerTokens(s) {
    return String(s || "").split(/[\n,]+/).map((t) => t.trim()).filter(Boolean);
}

// opts: {
//   title, multi,
//   keyOf(item)->str, primaryOf(item)->str, secondaryOf(item)->str,
//   exactKeyOf(item)->str,           // value compared in multi-value exact mode
//                                    //   (defaults to keyOf; client-side only)
//   selectedKeys[], seedItems[],     // seedItems: full objects already selected
//   items[] | search(query)->Promise<items[]>,   // static OR server-side source
//   onConfirm(items[])               // multi: chosen items; single: [clicked]
// }
//
// Filtering: a single term does substring matching (as you type). Pasting a
// comma- or newline-separated list of 2+ values switches to EXACT matching on
// the key (case-insensitive) and shows only those rows. For server-backed
// pickers the raw filter string is handed to ``search`` which applies the same
// rule server-side.
export function openListPicker(opts) {
    const {
        title, multi = false, keyOf, primaryOf, secondaryOf = () => "",
        exactKeyOf = keyOf,
        selectedKeys = [], seedItems = [], items = null, search = null, onConfirm,
    } = opts;

    const selected = new Set([...selectedKeys].map(String));
    // Remember every item we've seen (across searches) so OK can reconstruct
    // full objects even for selections not in the currently-shown results.
    const seen = new Map();
    const remember = (arr) => { for (const it of arr || []) seen.set(String(keyOf(it)), it); };
    remember(seedItems);
    if (items) remember(items);

    const overlay = document.createElement("div");
    overlay.className = "modal-backdrop";
    overlay.style.display = "flex";
    overlay.innerHTML = `
        <div class="modal" style="max-width:560px;">
            <div class="modal-header">
                <h3>${escHtml(title)}</h3>
                <button type="button" class="btn-text picker-close">×</button>
            </div>
            <div class="modal-body">
                ${multi
                    ? `<textarea class="picker-filter picker-filter-multi" rows="2"
                        placeholder="Filter… or paste a comma / newline-separated list for exact matches"></textarea>`
                    : `<input type="search" class="picker-filter" placeholder="Filter…">`}
                <ul class="picker-list"></ul>
                <p class="picker-more hint" hidden></p>
            </div>
            ${multi ? `<div class="actions actions-right" style="padding:8px 16px;">
                <button type="button" class="btn btn-secondary picker-cancel">Cancel</button>
                <button type="button" class="btn btn-primary picker-ok">Use selection</button>
            </div>` : ""}
        </div>`;
    document.body.appendChild(overlay);
    const list = overlay.querySelector(".picker-list");
    const moreHint = overlay.querySelector(".picker-more");
    const filterInput = overlay.querySelector(".picker-filter");
    const close = () => overlay.remove();

    // Render an array of items (already filtered) — capped, single write.
    function render(arr) {
        let html = "";
        const shown = Math.min(arr.length, PICKER_RENDER_CAP);
        for (let i = 0; i < shown; i++) {
            const it = arr[i];
            const key = escHtml(keyOf(it));
            const primary = escHtml(primaryOf(it) || keyOf(it));
            const sec = secondaryOf(it) || "";
            const subHtml = sec ? ` <span class="picker-sub">${escHtml(sec)}</span>` : "";
            const inner = `<span class="picker-name">${primary}</span>${subHtml}`;
            if (multi) {
                const checked = selected.has(String(keyOf(it))) ? " checked" : "";
                html += `<li data-key="${key}"><label><input type="checkbox"${checked}>`
                    + `<span>${inner}</span></label></li>`;
            } else {
                html += `<li class="picker-row" data-key="${key}">${inner}</li>`;
            }
        }
        list.innerHTML = html;
        return { shown, total: arr.length };
    }

    // Multi-value exact matches are auto-selected (checked) so pasting a list
    // selects those rows; the user then unticks any they don't want.
    function autoSelect(arr) {
        if (multi) for (const it of arr) selected.add(String(keyOf(it)));
    }

    let reqSeq = 0;
    async function load(query) {
        const tokens = parsePickerTokens(query);
        const exactMode = tokens.length >= 2;
        if (search) {
            const mine = ++reqSeq;
            moreHint.hidden = false;
            moreHint.textContent = "Searching…";
            let arr;
            try {
                arr = await search(query);
            } catch (err) {
                if (mine === reqSeq) { moreHint.hidden = false; moreHint.textContent = err.message; }
                return;
            }
            if (mine !== reqSeq) return;  // a newer keystroke superseded this one
            remember(arr);
            if (exactMode) autoSelect(arr);
            render(arr);
            if (!arr.length) {
                moreHint.hidden = false;
                moreHint.textContent = exactMode
                    ? "No exact matches for the pasted list." : "No matches.";
            } else if (exactMode) {
                moreHint.hidden = false;
                moreHint.textContent =
                    `${arr.length} matched and selected — untick any you don't want.`;
            } else if (arr.length >= PICKER_SEARCH_LIMIT) {
                moreHint.hidden = false;
                moreHint.textContent =
                    `Showing first ${arr.length} matches — type to narrow.`;
            } else {
                moreHint.hidden = true;
            }
        } else {
            let arr;
            if (exactMode) {
                // Multi-value: exact, case-insensitive key match only.
                const want = new Set(tokens.map((t) => t.toLowerCase()));
                arr = items.filter((it) => want.has(String(exactKeyOf(it)).toLowerCase()));
                autoSelect(arr);
            } else {
                const q = (tokens[0] || "").toLowerCase();
                arr = q
                    ? items.filter((it) =>
                        `${keyOf(it)} ${primaryOf(it)} ${secondaryOf(it)}`.toLowerCase().includes(q))
                    : items;
            }
            const { shown, total } = render(arr);
            if (exactMode) {
                moreHint.hidden = false;
                moreHint.textContent = total === 0
                    ? "No exact matches for the pasted list."
                    : `${total} matched and selected — untick any you don't want.`;
            } else if (total > shown) {
                moreHint.hidden = false;
                moreHint.textContent =
                    `Showing ${shown} of ${total} — refine the filter to narrow.`;
            } else {
                moreHint.hidden = true;
            }
        }
    }
    load("");

    let timer = null;
    filterInput.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => load(filterInput.value), search ? 250 : 150);
    });

    if (multi) {
        // One delegated listener for every checkbox — toggling never re-renders.
        list.addEventListener("change", (e) => {
            const cb = e.target;
            if (!cb || cb.tagName !== "INPUT") return;
            const li = cb.closest("li[data-key]");
            if (!li) return;
            if (cb.checked) selected.add(li.dataset.key);
            else selected.delete(li.dataset.key);
        });
        overlay.querySelector(".picker-ok").addEventListener("click", () => {
            const chosen = [...selected].map((k) => seen.get(k)).filter(Boolean);
            onConfirm(chosen);
            close();
        });
        overlay.querySelector(".picker-cancel").addEventListener("click", close);
    } else {
        list.addEventListener("click", (e) => {
            const li = e.target.closest("li[data-key]");
            if (!li) return;
            const it = seen.get(li.dataset.key);
            if (it) onConfirm([it]);
            close();
        });
    }
    overlay.querySelector(".picker-close").addEventListener("click", close);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    filterInput.focus();
}

// Run an async picker-open with a "Loading…" state on its trigger button, so
// the (possibly slow) initial fetch is legible and the button can't be
// double-clicked into parallel fetches.
export async function withPickerButtonBusy(btn, fn) {
    const prevLabel = btn.textContent;
    const prevDisabled = btn.disabled;
    btn.disabled = true;
    btn.textContent = "Loading…";
    try {
        await fn();
    } catch (err) {
        alert(err.message);
    } finally {
        btn.textContent = prevLabel;
        btn.disabled = prevDisabled;
    }
}
