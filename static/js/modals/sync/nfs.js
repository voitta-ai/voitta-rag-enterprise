// ---------------------------------------------------------------------------
// NFS picker — tree with 3-state checkboxes
//
// Selection is stored as a canonical set of POSIX paths: never two
// paths where one is the ancestor of the other (the ancestor wins;
// the descendants are pruned). The user-visible interaction is
// "click a checkbox to (de)select that subtree"; lazy-load children
// on expand so a 100k-directory NFS share doesn't pre-fetch.
// ---------------------------------------------------------------------------

import { api } from "../../api.js";
import { registerSource } from "./registry.js";
import { $ } from "./state.js";

let nfsAvailable = false;  // last status probe
const nfsSelected = new Set();  // canonical set of rel_paths
const nfsChildrenCache = new Map();  // rel_path -> [{name, rel_path}]
// Track which list-elements need their checkbox state recomputed when
// the selection changes (each node's render decides its own state based
// on `nfsSelected`, but recomputing visible nodes is cheap enough).
const nfsVisibleNodes = new Map();  // rel_path -> <li> element

async function nfsRefreshStatus() {
    try {
        const s = await api.nfsStatus();
        nfsAvailable = !!s.available;
        const opt = $("#sync-type-option-nfs");
        if (opt) opt.hidden = !s.available;
        const rootDisplay = $("#sync-nfs-root-display");
        if (rootDisplay) {
            rootDisplay.value = s.nfs_root || "";
            rootDisplay.placeholder = s.available
                ? ""
                : s.nfs_root
                    ? `${s.nfs_root} — unavailable (${s.status})`
                    : "(set the NFS root in Admin → Storage)";
        }
        return s;
    } catch (err) {
        nfsAvailable = false;
        const opt = $("#sync-type-option-nfs");
        if (opt) opt.hidden = true;
        return { available: false, status: "error" };
    }
}

// ---- Canonical-set operations ----

function nfsIsAncestorOrSelf(ancestor, candidate) {
    if (ancestor === "") return true;            // root covers everything
    if (ancestor === candidate) return true;
    return candidate.startsWith(ancestor + "/");
}

function nfsIsCovered(rel) {
    for (const sel of nfsSelected) if (nfsIsAncestorOrSelf(sel, rel)) return true;
    return false;
}

function nfsHasSelectedDescendant(rel) {
    if (rel === "") return nfsSelected.size > 0;
    for (const sel of nfsSelected) {
        if (sel === rel) continue;
        if (sel.startsWith(rel + "/")) return true;
    }
    return false;
}

// Three-state report for a given rel_path.
//   "checked"       = the whole subtree is selected (the path itself
//                     OR an ancestor is in the set)
//   "indeterminate" = some descendants are selected but not the whole
//   "unchecked"     = no overlap
function nfsNodeState(rel) {
    if (nfsIsCovered(rel)) return "checked";
    if (nfsHasSelectedDescendant(rel)) return "indeterminate";
    return "unchecked";
}

function nfsSelect(rel) {
    // Adding ``rel`` to the set means: drop any descendants of ``rel``
    // that were previously selected (they're redundant), and skip the
    // add if an ancestor already covers ``rel``.
    for (const sel of nfsSelected) if (nfsIsAncestorOrSelf(sel, rel)) return;
    for (const sel of [...nfsSelected]) {
        if (sel !== rel && sel.startsWith(rel + "/")) nfsSelected.delete(sel);
    }
    nfsSelected.add(rel);
}

function nfsDeselect(rel) {
    // If ``rel`` is directly in the set, drop it.
    if (nfsSelected.has(rel)) { nfsSelected.delete(rel); return; }
    // Otherwise an ancestor covers it — we need to split the ancestor
    // into its siblings minus ``rel``. Because we lazy-load, we may
    // not have the ancestor's children in cache; in that case we fall
    // back to "drop the ancestor entirely" (user can re-pick siblings).
    let covering = "";
    for (const sel of nfsSelected) {
        if (nfsIsAncestorOrSelf(sel, rel)) { covering = sel; break; }
    }
    if (!covering && !nfsSelected.has("")) return;  // nothing to do
    nfsSelected.delete(covering);
    // Walk down from ``covering`` to ``rel``, re-selecting siblings of
    // each step we descend into. For each ancestor between covering
    // and rel, fetch (or use cached) children and add every sibling
    // that's NOT the path we're descending into.
    nfsExpandCoverage(covering, rel).catch(() => {});
}

async function nfsExpandCoverage(coveringPath, removePath) {
    // Walk the chain ``coveringPath → removePath`` one segment at a time.
    let current = coveringPath;
    const segs = removePath.slice(coveringPath.length).replace(/^\//, "").split("/");
    for (const seg of segs) {
        const next = current ? `${current}/${seg}` : seg;
        const children = await nfsFetchChildren(current);
        for (const child of children) {
            if (child.rel_path !== next) {
                // Skip if anything already covers this sibling (rare).
                if (!nfsIsCovered(child.rel_path)) nfsSelected.add(child.rel_path);
            }
        }
        current = next;
    }
    nfsRefreshTreeUi();
}

// ---- Tree rendering ----

async function nfsFetchChildren(rel) {
    if (nfsChildrenCache.has(rel)) return nfsChildrenCache.get(rel);
    const out = await api.nfsBrowse(rel);
    const entries = out.entries || [];
    nfsChildrenCache.set(rel, entries);
    return entries;
}

function nfsBuildLi(rel, name, level) {
    const li = document.createElement("li");
    li.dataset.relPath = rel;
    li.dataset.level = String(level);
    li.style.padding = "2px 0 2px " + (level * 14) + "px";
    li.style.listStyle = "none";

    const toggle = document.createElement("span");
    toggle.className = "nfs-toggle";
    toggle.textContent = "▶";
    toggle.style.cursor = "pointer";
    toggle.style.marginRight = "4px";
    toggle.style.display = "inline-block";
    toggle.style.width = "12px";
    toggle.style.userSelect = "none";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.style.marginRight = "6px";

    const label = document.createElement("span");
    label.textContent = name || "(root)";
    label.style.cursor = "pointer";
    label.title = rel || "(root)";

    li.append(toggle, cb, label);

    const childUl = document.createElement("ul");
    childUl.style.listStyle = "none";
    childUl.style.padding = "0";
    childUl.style.margin = "0";
    childUl.hidden = true;
    li.append(childUl);

    // Apply current state.
    nfsApplyCheckboxState(cb, nfsNodeState(rel));

    // Cached promise of "this node's children are fully built in the
    // DOM". Used by both the user-click expand path and the rehydrate
    // walker — neither has to second-guess whether the API call is
    // still in flight; await the promise and proceed.
    let loadedPromise = null;
    async function ensureLoaded() {
        if (loadedPromise) return loadedPromise;
        loadedPromise = (async () => {
            try {
                const children = await nfsFetchChildren(rel);
                if (!children.length) {
                    const empty = document.createElement("li");
                    empty.style.padding = "2px 0 2px " + ((level + 1) * 14) + "px";
                    empty.className = "muted";
                    empty.textContent = "(empty)";
                    childUl.append(empty);
                } else {
                    for (const child of children) {
                        const childLi = nfsBuildLi(child.rel_path, child.name, level + 1);
                        childUl.append(childLi);
                    }
                }
            } catch (err) {
                const errLi = document.createElement("li");
                errLi.style.padding = "2px 0 2px " + ((level + 1) * 14) + "px";
                errLi.style.color = "#dc3545";
                errLi.textContent = `error: ${err.message}`;
                childUl.append(errLi);
            }
        })();
        return loadedPromise;
    }
    async function expand() {
        await ensureLoaded();
        childUl.hidden = false;
        toggle.textContent = "▼";
    }
    function collapse() {
        childUl.hidden = true;
        toggle.textContent = "▶";
    }

    // Expose to nfsExpandPath so the rehydrate walker can await the
    // exact same load path the click handler uses — no second
    // implementation, no setTimeout races.
    li.__nfsExpand = expand;

    toggle.addEventListener("click", () => {
        if (childUl.hidden) expand(); else collapse();
    });
    label.addEventListener("click", () => {
        if (childUl.hidden) expand(); else collapse();
    });
    cb.addEventListener("change", () => {
        if (cb.checked) nfsSelect(rel);
        else nfsDeselect(rel);
        nfsRefreshTreeUi();
        nfsUpdateCount();
    });

    nfsVisibleNodes.set(rel, li);
    return li;
}

function nfsApplyCheckboxState(cb, state) {
    if (state === "checked") {
        cb.checked = true;
        cb.indeterminate = false;
    } else if (state === "indeterminate") {
        cb.checked = false;
        cb.indeterminate = true;
    } else {
        cb.checked = false;
        cb.indeterminate = false;
    }
}

function nfsRefreshTreeUi() {
    for (const [rel, li] of nfsVisibleNodes) {
        const cb = li.querySelector(":scope > input[type=checkbox]");
        if (cb) nfsApplyCheckboxState(cb, nfsNodeState(rel));
    }
}

function nfsUpdateCount() {
    const count = $("#sync-nfs-count");
    if (!count) return;
    if (nfsSelected.size === 0) {
        count.textContent = "none selected";
    } else if (nfsSelected.has("")) {
        count.textContent = "entire NFS root selected";
    } else {
        count.textContent = `${nfsSelected.size} folder${nfsSelected.size === 1 ? "" : "s"} selected`;
    }
}

// Serialise rebuilds: setSyncType("nfs") fires one off in the
// background, and loadSyncSource fires another with the real saved
// selection a few ms later. If they race, both append a root node and
// the user sees "folder multiplication". Chaining off this promise
// guarantees rebuild N runs strictly after rebuild N-1 finishes, so the
// later call's DOM wipe (treeUl.innerHTML = "") correctly clears the
// earlier call's output.
let nfsRebuildChain = Promise.resolve();

function nfsRebuildTree(initialSelection = []) {
    nfsRebuildChain = nfsRebuildChain
        .catch(() => {})
        .then(() => _nfsRebuildTreeImpl(initialSelection));
    return nfsRebuildChain;
}

async function _nfsRebuildTreeImpl(initialSelection) {
    nfsSelected.clear();
    nfsVisibleNodes.clear();
    nfsChildrenCache.clear();
    for (const s of initialSelection) nfsSelected.add(s);
    const treeUl = $("#sync-nfs-tree");
    treeUl.innerHTML = "";
    if (!nfsAvailable) {
        const li = document.createElement("li");
        li.className = "muted";
        li.style.padding = "8px";
        li.textContent = "NFS is unavailable — ask an admin to configure the NFS root.";
        treeUl.append(li);
        nfsUpdateCount();
        return;
    }
    // Add the synthetic root node so the user can pick "entire root".
    const rootLi = nfsBuildLi("", "(NFS root)", 0);
    treeUl.append(rootLi);
    // Auto-expand to reveal any pre-selected paths so the user sees
    // their saved selection without hunting through the tree.
    for (const sel of initialSelection) {
        if (sel === "") continue;
        await nfsExpandPath(sel);
    }
    nfsRefreshTreeUi();
    nfsUpdateCount();
}

async function nfsExpandPath(targetRel) {
    // Walk from the synthetic root down to ``targetRel``, awaiting
    // each ancestor's expand promise before descending. This is the
    // rehydrate path — when the modal opens with saved selection, we
    // need every ancestor's children in the DOM (and the checkbox
    // state recomputed) before the user starts clicking.
    //
    // Previously this called toggle.click() and waited setTimeout(0)
    // for children to render. That's a race: if the API call hadn't
    // resolved yet, nfsVisibleNodes.get(next) returned undefined and
    // the walk silently aborted — leaving the tree half-expanded and
    // checkbox state stale, which looked like "folder multiplication"
    // to the user. Now we await li.__nfsExpand() (the same code path
    // the click handler runs) — deterministic, no setTimeout.
    const segs = targetRel.split("/");
    let current = "";
    for (const seg of segs) {
        const li = nfsVisibleNodes.get(current);
        if (!li || typeof li.__nfsExpand !== "function") return;
        await li.__nfsExpand();
        current = current ? `${current}/${seg}` : seg;
    }
}

$("#sync-nfs-clear").addEventListener("click", () => {
    nfsSelected.clear();
    nfsRefreshTreeUi();
    nfsUpdateCount();
});

function nfsOnShow() {
    nfsRefreshStatus().then(() => {
        // Rebuild with whatever is already in nfsSelected (set by
        // loadSyncSource for existing rows, or empty for new ones).
        const initial = [...nfsSelected];
        nfsRebuildTree(initial);
    });
}

async function loadNfsForm(src) {
    if (!src.nfs) return;
    // Re-probe status so the option visibility is fresh; then
    // populate the saved subpath and walk to it. If the admin
    // disabled NFS since the row was saved, the picker shows
    // an unavailable banner — the user can still see what was
    // configured but cannot trigger a sync.
    await nfsRefreshStatus();
    const subpaths = Array.isArray(src.nfs.subpaths) && src.nfs.subpaths.length
        ? src.nfs.subpaths
        : (src.nfs.subpath ? [src.nfs.subpath] : []);
    await nfsRebuildTree(subpaths);
}

function nfsFormConfig() {
    const subpaths = [...nfsSelected];
    // ``subpath`` kept for backwards-compat with the old server.
    return { subpath: subpaths[0] || "", subpaths };
}

registerSource({
    type: "nfs",
    tab: "nfs",
    paneId: "#sync-form-nfs",
    reset: nfsRefreshStatus,
    onShow: nfsOnShow,
    load: loadNfsForm,
    formConfig: nfsFormConfig,
});
