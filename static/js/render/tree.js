// Folder list / file tree rendering.
//
// Keyed reconciliation: renders update existing <li> elements in place
// instead of tearing the list down and rebuilding it. The hover/active
// pseudo-states stay attached to live nodes, mid-click mousedown→mouseup
// pairs land on the same target, and the UI feels OS-native under heavy
// indexing.
//
// Cache keys:
//   "root:<folder_id>"            — top-level folder row
//   "dir:<folder_id>:<rel_dir>"   — subdirectory row inside an expanded folder
//   "file:<file_id>"              — leaf file row
//   "empty"                       — the "No folders yet" placeholder
//
// Rows are created via build*Row(); their mutable bits (text, status
// classes, selected state, chevron rotation, switch checked-ness) are
// reapplied via update*Row() on every render. Event handlers attach
// once at create time and read fresh state from module-level stores +
// dataset attributes — never closed-over render-time props.

import { api } from "../api.js";
import { reconcileChildren, setIfChanged } from "../dom/reconcile.js";
import { buildSwitch } from "../dom/switch.js";
import {
    iconForDirKind,
    iconForFile,
    iconForFolder,
    iconForSource,
    lockBadgeIcon,
    sourceNeedsLockBadge,
} from "./icons.js";

// Helper — set the <img> src in place if and only if it changed.
// Bypasses iconForX → string → src loops, and stops the browser from
// re-decoding the SVG on every reconcile pass.
function setIconSrc(img, src) {
    if (img.getAttribute("src") !== src) img.setAttribute("src", src);
}

// Show / hide a small lock badge layered over the source glyph.
// Used for the "github_private" data source — the octocat plus a
// padlock corner is the visual cue most users already know from
// GitHub itself. The badge is a separate <img> appended on first
// activation; subsequent updates only toggle its hidden state.
function _applyLockBadge(glyphSpan, on) {
    let badge = glyphSpan.querySelector(".source-lock");
    if (on) {
        if (!badge) {
            badge = document.createElement("img");
            badge.className = "source-lock";
            badge.alt = "";
            badge.src = lockBadgeIcon();
            glyphSpan.append(badge);
        }
        badge.hidden = false;
    } else if (badge) {
        badge.hidden = true;
    }
}
import {
    getSelectedArtifact,
    getSelectedFileId,
    getSelectedFolderId,
    getSelectedRelDir,
    isExpanded,
    isFileExpanded,
    removeGhostDir,
    selectArtifact,
    selectNode,
    toggleExpanded,
    toggleFileExpanded,
} from "../flows/selection.js";
import { scheduleFullRender } from "./render-loop.js";
import {
    buildTree,
    summariseSubtree,
    userStateLabel,
} from "../flows/tree-model.js";
import { activeFolders, files, folders } from "../store.js";

const $ = (sel) => document.querySelector(sel);

let _folderFilter = "";

export function setFolderFilter(q) { _folderFilter = q; }

export function renderFoldersFiltered() {
    const q = _folderFilter.trim().toLowerCase();
    const list = q ? folders.get().filter((f) => f.display_name.toLowerCase().includes(q)) : folders.get();
    renderFolders(list);
}

const rowCache = new Map();

// File types whose page renders are exposed as artifact children.
const _EXPANDABLE_EXTS = new Set([".pdf", ".pptx", ".ppt", ".docx", ".doc", ".odp", ".odt"]);

function _isExpandable(relPath) {
    const lower = relPath.toLowerCase();
    const dot = lower.lastIndexOf(".");
    return dot >= 0 && _EXPANDABLE_EXTS.has(lower.slice(dot));
}

// Lazy artifact caches: fileId → undefined (unfetched) | "loading" | item[]
const _imageCache = new Map();
const _layoutCache = new Map();

function _loadArtifacts(fileId) {
    if (!_imageCache.has(fileId)) {
        _imageCache.set(fileId, "loading");
        api.fileImages(fileId)
            .then((imgs) => { _imageCache.set(fileId, imgs); renderFoldersFiltered(); })
            .catch(() => { _imageCache.set(fileId, []); });
    }
    if (!_layoutCache.has(fileId)) {
        _layoutCache.set(fileId, "loading");
        api.fileLayout(fileId)
            .then((blocks) => { _layoutCache.set(fileId, blocks); renderFoldersFiltered(); })
            .catch(() => { _layoutCache.set(fileId, []); });
    }
}

// ---------------------------------------------------------------------------
// Row builders (created once per identity, kept across renders)
// ---------------------------------------------------------------------------

function buildFolderRoot(folderId) {
    const li = document.createElement("li");
    li.className = "tree-row folder-root";
    li.dataset.folderId = String(folderId);
    li.dataset.relDir = "";

    const nameCell = document.createElement("span");
    nameCell.className = "name-cell";
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▸";
    chevron.addEventListener("click", onChevronClick);
    const label = document.createElement("span");
    label.className = "label";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    const img = document.createElement("img");
    img.className = "icon-img";
    img.alt = "";
    // Initial src — overwritten on first updateTreeRow once the
    // sync_source_kind is known.
    img.src = iconForSource("regular");
    glyph.append(img);
    const text = document.createElement("span");
    label.append(glyph, text);
    nameCell.append(chevron, label);

    const fileCount = document.createElement("span");
    fileCount.className = "num";
    const indexedCount = document.createElement("span");
    indexedCount.className = "num";
    const tag = document.createElement("span");
    tag.className = "status-tag";
    const slot1 = document.createElement("span");
    const slot2 = document.createElement("span");

    li.append(nameCell, fileCount, indexedCount, tag, slot1, slot2);
    li.addEventListener("click", onRowClick);

    li._refs = { nameCell, chevron, label, glyph, img, text, fileCount, indexedCount, tag, slot1, slot2 };
    li._activeSwitch = null;
    li._shareSwitch = null;
    li._isRoot = true;
    li._sourceKind = null;
    return li;
}

function _buildDeleteBtn(onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tree-delete-btn";
    btn.title = "Delete";
    btn.textContent = "×";
    btn.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return btn;
}

function buildDirRow(folderId, relDir) {
    const li = document.createElement("li");
    li.className = "tree-row dir";
    li.dataset.folderId = String(folderId);
    li.dataset.relDir = relDir;

    const nameCell = document.createElement("span");
    nameCell.className = "name-cell";
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▸";
    chevron.addEventListener("click", onChevronClick);
    const label = document.createElement("span");
    label.className = "label";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    const img = document.createElement("img");
    img.className = "icon-img";
    img.alt = "";
    img.src = iconForFolder();
    glyph.append(img);
    const text = document.createElement("span");
    label.append(glyph, text);
    nameCell.append(chevron, label);

    const fileCount = document.createElement("span");
    fileCount.className = "num";
    const indexedCount = document.createElement("span");
    indexedCount.className = "num";
    const tag = document.createElement("span");
    tag.className = "status-tag";

    const spacer1 = document.createElement("span");
    spacer1.style.visibility = "hidden";
    const spacer2 = document.createElement("span");
    spacer2.style.visibility = "hidden";
    const delBtn = _buildDeleteBtn(() => onDeleteDir(li));
    delBtn.hidden = true;

    // spacer1 + spacer2 keep the 6-column grid aligned with root rows.
    // delBtn is position:absolute so it doesn't add a 7th grid column.
    li.append(nameCell, fileCount, indexedCount, tag, spacer1, spacer2, delBtn);
    li.addEventListener("click", onRowClick);

    li._refs = { nameCell, chevron, glyph, img, text, fileCount, indexedCount, tag, delBtn };
    li._isRoot = false;
    li._dirKind = null;
    li._canDelete = false;
    return li;
}

function buildFileRow(fileId) {
    const li = document.createElement("li");
    li.className = "tree-row file";
    li.dataset.fileId = String(fileId);

    const nameCell = document.createElement("span");
    nameCell.className = "name-cell";
    const chevron = document.createElement("span");
    chevron.className = "chevron leaf";
    chevron.textContent = "·";
    chevron.addEventListener("click", onChevronClick);
    const label = document.createElement("span");
    label.className = "label";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    const img = document.createElement("img");
    img.className = "icon-img";
    img.alt = "";
    glyph.append(img);
    const text = document.createElement("span");
    label.append(glyph, text);
    nameCell.append(chevron, label);

    const blank1 = document.createElement("span");
    const blank2 = document.createElement("span");
    const tag = document.createElement("span");
    tag.className = "status-tag";
    const delBtn = _buildDeleteBtn(() => onDeleteFile(li));
    delBtn.hidden = true;

    li.append(nameCell, blank1, blank2, tag, delBtn);
    li.addEventListener("click", onRowClick);

    li._refs = { nameCell, chevron, label, glyph, img, text, tag, delBtn };
    li._fileExtKey = null;
    li._canDelete = false;
    return li;
}

// One summary row per artifact type (images / layout) — clicking opens
// the detail view in the preview pane rather than flooding the tree.
function buildArtifactSummaryRow(fileId, type) {
    const li = document.createElement("li");
    li.className = `tree-row artifact artifact-${type}`;
    li.dataset.fileId = String(fileId);
    li.dataset.artifactType = type;

    const nameCell = document.createElement("span");
    nameCell.className = "name-cell";
    const chevron = document.createElement("span");
    chevron.className = "chevron leaf";
    chevron.textContent = "·";
    const label = document.createElement("span");
    label.className = "label artifact-label";
    const icon = document.createElement("span");
    icon.className = "artifact-summary-icon";
    icon.textContent = type === "images" ? "📷" : "⊞";
    const text = document.createElement("span");
    text.className = "artifact-summary-text";
    label.append(icon, text);
    nameCell.append(chevron, label);
    li.append(nameCell);
    li.addEventListener("click", onArtifactClick);
    li._refs = { nameCell, icon, text };
    return li;
}

// Click handlers read fresh data from the DOM/stores so closures captured
// at create time never go stale across renders.
function onChevronClick(e) {
    e.stopPropagation();
    const li = e.currentTarget.closest(".tree-row");
    if (!li) return;
    if (e.currentTarget.classList.contains("leaf")) return;
    if (li.dataset.fileId) {
        toggleFileExpanded(Number(li.dataset.fileId));
    } else {
        const folderId = Number(li.dataset.folderId);
        const relDir = li.dataset.relDir || "";
        toggleExpanded(folderId, relDir);
    }
    renderFoldersFiltered();
}

function onArtifactClick(e) {
    const li = e.currentTarget;
    const fileId = Number(li.dataset.fileId);
    const type = li.dataset.artifactType;
    const folderId = Number(li.dataset.folderId);
    const relDir = li.dataset.relDir || "";
    selectArtifact(folderId, relDir, fileId, type);
}

function onRowClick(e) {
    const li = e.currentTarget;
    if (li.dataset.fileId) {
        selectNode(Number(li.dataset.folderId), li.dataset.relDir || "", Number(li.dataset.fileId));
    } else {
        selectNode(Number(li.dataset.folderId), li.dataset.relDir || "");
    }
}

// ---------------------------------------------------------------------------
// Row updaters (mutate in place across renders)
// ---------------------------------------------------------------------------

function updateTreeRow(li, { folder, displayName, depth, isOpen, hasChildren, isSelected, summary, sharedReadonly, dirKind, canDelete }) {
    const r = li._refs;
    const kindClass = dirKind ? ` dir-kind-${dirKind}` : "";
    const baseClass = (li._isRoot ? "tree-row folder-root" : "tree-row dir") + kindClass;
    const cls =
        baseClass +
        (isSelected ? " selected" : "") +
        (sharedReadonly ? " shared-readonly" : "");
    setIfChanged(li, "className", cls);

    const pad = depth > 0 ? `${depth * 14}px` : "";
    if (r.nameCell.style.paddingLeft !== pad) r.nameCell.style.paddingLeft = pad;

    let chevCls = "chevron";
    if (isOpen) chevCls += " open";
    if (!hasChildren) chevCls += " leaf";
    setIfChanged(r.chevron, "className", chevCls);

    // Source kind icon on top-level rows; dir-kind / folder on
    // nested ones. Update via src swap so the browser keeps the
    // <img> cached and the DOM node never moves.
    if (li._isRoot) {
        const sk = folder.sync_source_kind || "regular";
        if (li._sourceKind !== sk) {
            setIconSrc(r.img, iconForSource(sk));
            li._sourceKind = sk;
            _applyLockBadge(r.glyph, sourceNeedsLockBadge(sk));
        }
    } else if (li._dirKind !== (dirKind || null)) {
        const dirIcon = iconForDirKind(dirKind) || iconForFolder();
        setIconSrc(r.img, dirIcon);
        li._dirKind = dirKind || null;
    }

    setIfChanged(r.text, "textContent", displayName);

    const total = summary.total || 0;
    setIfChanged(r.fileCount, "textContent", total ? String(total) : "");
    setIfChanged(
        r.indexedCount,
        "textContent",
        total ? `${summary.indexed}${summary.errored ? ` · ${summary.errored}!` : ""}` : "",
    );
    setIfChanged(r.tag, "className", `status-tag ${summary.status}`);
    setIfChanged(r.tag, "textContent", summary.status);

    if (li._isRoot) {
        updateRootSwitches(li, folder);
    } else if (li._refs.delBtn) {
        const show = !!canDelete;
        if (li._refs.delBtn.hidden === show) li._refs.delBtn.hidden = !show;
        li._canDelete = show;
    }
}

function updateRootSwitches(li, folder) {
    const r = li._refs;

    // MCP-search toggle (slot1) — present on every root, regardless of ownership.
    if (!li._activeSwitch) {
        const sw = buildSwitch({
            title: "",
            checked: folder.active,
            disabled: false,
            onChange: (next) => toggleFolderActive(folder, next),
        });
        r.slot1.replaceWith(sw);
        r.slot1 = sw;
        li._activeSwitch = sw.querySelector("input");
    }
    setIfChanged(li._activeSwitch, "checked", !!folder.active);
    const activeTitle = folder.active
        ? "MCP search includes this folder. Click to exclude."
        : "MCP search excludes this folder. Click to include.";
    if (r.slot1.title !== activeTitle) r.slot1.title = activeTitle;

    // Share toggle (slot2) — only for owners; non-owners get an invisible spacer.
    if (folder.owned) {
        if (!li._shareSwitch) {
            const sw = buildSwitch({
                title: "",
                checked: folder.shared,
                disabled: false,
                onChange: (next) => toggleFolderShare(folder, next),
            });
            r.slot2.replaceWith(sw);
            r.slot2 = sw;
            li._shareSwitch = sw.querySelector("input");
        }
        setIfChanged(li._shareSwitch, "checked", !!folder.shared);
        const shareTitle = folder.shared
            ? "Folder is shared with everyone. Click to unshare."
            : "Folder is private. Click to share with everyone.";
        if (r.slot2.title !== shareTitle) r.slot2.title = shareTitle;
    } else if (li._shareSwitch) {
        // Owner status flipped from owned to not-owned (rare but possible
        // if a folder is transferred). Drop the switch back to a spacer.
        const spacer = document.createElement("span");
        spacer.className = "folder-switch";
        spacer.style.visibility = "hidden";
        r.slot2.replaceWith(spacer);
        r.slot2 = spacer;
        li._shareSwitch = null;
    }
}

function updateArtifactSummaryRow(li, { file, type, count, depth }) {
    const r = li._refs;
    const pad = `${depth * 14}px`;
    if (r.nameCell.style.paddingLeft !== pad) r.nameCell.style.paddingLeft = pad;
    const label = count == null ? "…"
        : type === "images" ? `Images · ${count}`
        : `Layout · ${count} block${count === 1 ? "" : "s"}`;
    setIfChanged(r.text, "textContent", label);
    const sel = getSelectedArtifact();
    const isSelected = file.id === getSelectedFileId() && sel?.type === type;
    setIfChanged(li, "className", `tree-row artifact artifact-${type}${isSelected ? " selected" : ""}`);
}

function updateFileRow(li, { file, depth }) {
    const r = li._refs;
    const pad = depth > 0 ? `${depth * 14}px` : "";
    if (r.nameCell.style.paddingLeft !== pad) r.nameCell.style.paddingLeft = pad;
    const isSelected = file.id === getSelectedFileId() && getSelectedArtifact() === null;
    setIfChanged(li, "className", `tree-row file${isSelected ? " selected" : ""}`);

    const expandable = _isExpandable(file.rel_path);
    const fileOpen = expandable && isFileExpanded(file.id);
    let chevCls = "chevron";
    if (!expandable) chevCls += " leaf";
    else if (fileOpen) chevCls += " open";
    setIfChanged(r.chevron, "className", chevCls);
    setIfChanged(r.chevron, "textContent", expandable ? "▸" : "·");

    const basename = file.rel_path.split("/").pop();
    setIfChanged(r.text, "textContent", basename);

    // The extension is the only thing affecting the icon — gate
    // innerHTML writes on it so renames within the same extension
    // (rare) don't churn DOM.
    const lower = file.rel_path.toLowerCase();
    const dot = lower.lastIndexOf(".");
    let extKey = dot >= 0 ? lower.slice(dot) : "";
    if (lower.endsWith(".tar.gz") || lower.endsWith(".tar.bz2") || lower.endsWith(".tar.xz")) {
        extKey = ".tar.gz";
    }
    if (li._fileExtKey !== extKey) {
        setIconSrc(r.img, iconForFile(file.rel_path));
        li._fileExtKey = extKey;
    }
    if (r.label.title !== file.rel_path) r.label.title = file.rel_path;
    const stateLabel = userStateLabel(file.state);
    setIfChanged(r.tag, "className", `status-tag ${stateLabel}`);
    setIfChanged(r.tag, "textContent", stateLabel);
    const tagTitle = `state=${file.state}, pending_embeds=${file.pending_embeds}`;
    if (r.tag.title !== tagTitle) r.tag.title = tagTitle;

    // Delete button — only for owned regular folders.
    const canDel = !!(li._canDelete);
    if (r.delBtn.hidden === canDel) r.delBtn.hidden = !canDel;
}

// ---------------------------------------------------------------------------
// Delete handlers (files + subdirs, regular folders only)
// ---------------------------------------------------------------------------

async function onDeleteFile(li) {
    const fileId = Number(li.dataset.fileId);
    const folderId = Number(li.dataset.folderId);
    const name = li._refs.text.textContent;
    if (!confirm(`Delete "${name}"?\n\nThis cannot be undone.`)) return;
    try {
        await api.deleteFile(folderId, fileId);
        files.update((list) => list.filter((f) => f.id !== fileId));
        scheduleFullRender();
    } catch (err) {
        alert(err.message);
    }
}

async function onDeleteDir(li) {
    const folderId = Number(li.dataset.folderId);
    const rel = li.dataset.relDir;
    const name = li._refs.text.textContent;
    if (!confirm(`Delete folder "${name}" and all its contents?\n\nThis cannot be undone.`)) return;
    try {
        await api.deleteSubdir(folderId, rel);
        removeGhostDir(folderId, rel);
        const prefix = rel.endsWith("/") ? rel : rel + "/";
        files.update((list) => list.filter(
            (f) => f.folder_id !== folderId || (f.rel_path !== rel && !f.rel_path.startsWith(prefix))
        ));
        scheduleFullRender();
    } catch (err) {
        alert(err.message);
    }
}

// ---------------------------------------------------------------------------
// Folder-side switches (active in MCP search, shared with everyone)
// ---------------------------------------------------------------------------

async function toggleFolderActive(folder, active) {
    try {
        const updated = await api.setFolderActive(folder.id, active);
        const next = folders.get().map((f) => (f.id === folder.id ? updated : f));
        folders.set(next);
    } catch (err) {
        alert(err.message);
        renderFoldersFiltered(); // restore the original visual state
    }
}

async function toggleFolderShare(folder, shared) {
    try {
        const updated = await api.setFolderShare(folder.id, shared);
        const next = folders.get().map((f) => (f.id === folder.id ? updated : f));
        folders.set(next);
    } catch (err) {
        alert(err.message);
        renderFoldersFiltered();
    }
}

// ---------------------------------------------------------------------------
// Render entry point + helpers
// ---------------------------------------------------------------------------

function ensureEmptyRow() {
    let el = rowCache.get("empty");
    if (!el) {
        el = document.createElement("li");
        el.className = "tree-row";
        el.style.gridTemplateColumns = "1fr";
        el.style.color = "var(--color-text-secondary)";
        el.textContent = "No folders yet — create or add one above.";
        rowCache.set("empty", el);
    }
    return el;
}

export function renderFolders(list) {
    const ul = $("#folder-list");
    const sorted = [...list].sort((a, b) => a.id - b.id);
    if (sorted.length === 0) {
        const seenKeys = new Set(["empty"]);
        reconcileChildren(ul, [ensureEmptyRow()], seenKeys, rowCache);
        return;
    }
    rowCache.delete("empty");
    const allFiles = files.get();
    // Server-pushed set of folder_ids currently having queued/running
    // work. Read once per render so the snapshot is stable across the
    // tree traversal below; subscribers in boot.js trigger a re-render
    // when the set changes.
    const activeFolderSet = activeFolders.get();
    const targetRows = [];
    const seenKeys = new Set();

    for (const folder of sorted) {
        const folderFiles = allFiles.filter((x) => x.folder_id === folder.id);
        const tree = buildTree(folderFiles, folder.id);
        emitTreeRow({
            targetRows,
            seenKeys,
            folder,
            node: tree,
            relDir: "",
            displayName: folder.display_name,
            depth: 0,
            isRoot: true,
            folderActive: activeFolderSet.has(folder.id),
        });
    }
    reconcileChildren(ul, targetRows, seenKeys, rowCache);
}

function emitTreeRow({ targetRows, seenKeys, folder, node, relDir, displayName, depth, isRoot, folderActive }) {
    const summary = summariseSubtree(node, !!folderActive);
    const hasChildren = node.dirs.size > 0 || node.files.length > 0;
    const isOpen = isExpanded(folder.id, relDir);
    // Dir/root row counts as "selected" only when no file is selected.
    // Otherwise clicking a file inside this dir would highlight both the
    // file row and its parent dir row, which looks like two competing
    // selections.
    const isSelected = folder.id === getSelectedFolderId()
        && relDir === getSelectedRelDir()
        && getSelectedFileId() === null;
    const sharedReadonly = isRoot && folder.shared && !folder.owned;

    const cacheKey = isRoot ? `root:${folder.id}` : `dir:${folder.id}:${relDir}`;
    let li = rowCache.get(cacheKey);
    if (!li) {
        li = isRoot ? buildFolderRoot(folder.id) : buildDirRow(folder.id, relDir);
        rowCache.set(cacheKey, li);
    }
    // Roots use the folder badge regardless of descendants; only
    // nested dirs adopt the Google-Workspace icon when their .md
    // descendants identify a Drive-native source. (A root folder
    // can be the Drive mount itself — labelling it "spreadsheet"
    // because one of its files came from Sheets would be wrong.)
    const dirKind = isRoot ? null : (node.kind || null);
    const canDelete = !isRoot && !!(folder.owned) && (folder.sync_source_kind || "regular") === "regular";
    updateTreeRow(li, { folder, displayName, depth, isOpen, hasChildren, isSelected, summary, sharedReadonly, dirKind, canDelete });
    targetRows.push(li);
    seenKeys.add(cacheKey);

    if (!isOpen) return;
    for (const [name, child] of [...node.dirs.entries()].sort()) {
        emitTreeRow({
            targetRows,
            seenKeys,
            folder,
            node: child,
            relDir: relDir ? `${relDir}/${name}` : name,
            displayName: name,
            depth: depth + 1,
            isRoot: false,
            folderActive,
        });
    }
    const fileCanDelete = !!(folder.owned) && (folder.sync_source_kind || "regular") === "regular";
    for (const f of [...node.files].sort((a, b) => a.rel_path.localeCompare(b.rel_path))) {
        const fkey = `file:${f.id}`;
        let fli = rowCache.get(fkey);
        if (!fli) {
            fli = buildFileRow(f.id);
            rowCache.set(fkey, fli);
        }
        fli.dataset.folderId = String(folder.id);
        // relDir = parent dir of the file (empty string at folder root).
        const slashIdx = f.rel_path.lastIndexOf("/");
        fli.dataset.relDir = slashIdx >= 0 ? f.rel_path.slice(0, slashIdx) : "";
        fli._canDelete = fileCanDelete;
        updateFileRow(fli, { file: f, depth: depth + 1 });
        targetRows.push(fli);
        seenKeys.add(fkey);

        // Emit summary artifact rows for expandable files (one per type).
        if (_isExpandable(f.rel_path) && isFileExpanded(f.id)) {
            _loadArtifacts(f.id);
            const imgs = _imageCache.get(f.id);
            const blocks = _layoutCache.get(f.id);

            // Images summary row — always emit while expanded so user can see
            // "…" loading state, but skip once loaded if there are no images.
            const imgLoading = !Array.isArray(imgs);
            if (imgLoading || (Array.isArray(imgs) && imgs.length > 0)) {
                const akey = `artifact:images:${f.id}`;
                let ali = rowCache.get(akey);
                if (!ali) { ali = buildArtifactSummaryRow(f.id, "images"); rowCache.set(akey, ali); }
                ali.dataset.folderId = String(folder.id);
                ali.dataset.relDir = fli.dataset.relDir;
                updateArtifactSummaryRow(ali, { file: f, type: "images", count: imgLoading ? null : imgs.length, depth: depth + 2 });
                targetRows.push(ali);
                seenKeys.add(akey);
            }

            // Layout summary row — same logic.
            const layoutLoading = !Array.isArray(blocks);
            if (layoutLoading || (Array.isArray(blocks) && blocks.length > 0)) {
                const akey = `artifact:layout:${f.id}`;
                let ali = rowCache.get(akey);
                if (!ali) { ali = buildArtifactSummaryRow(f.id, "layout"); rowCache.set(akey, ali); }
                ali.dataset.folderId = String(folder.id);
                ali.dataset.relDir = fli.dataset.relDir;
                updateArtifactSummaryRow(ali, { file: f, type: "layout", count: layoutLoading ? null : blocks.length, depth: depth + 2 });
                targetRows.push(ali);
                seenKeys.add(akey);
            }
        }
    }
}
