// In-app assistant: launcher + floating chat window.
//
// Server side lives in services/assistant (see docs/OPERATIONS.md §11).
// Turns stream over /ws/assistant and are persisted server-side; this module
// renders the live stream, then reloads the stored transcript when a turn
// ends so what's on screen is exactly what was saved.
//
// Identity: while an admin impersonates someone, the window offers a
// Mine / Theirs toggle — your own conversation list or theirs. Tools always
// run with the impersonated account's access (like the rest of the app);
// the person typing is always recorded as the author.

import { api } from "../api.js";
import { expand, getSelectedFileId, getSelectedFolderId, selectNode } from "../flows/selection.js";
import { openSettings } from "../modals/settings.js";
import { assistantConfig, files, folders } from "../store.js";
import { AssistantSocket } from "./socket.js";
import { Transcript } from "./transcript.js";
import { createWindow } from "./window.js";

const $ = (sel) => document.querySelector(sel);

const state = {
    view: "mine",            // which conversation list: "mine" | "theirs"
    conversations: [],
    current: null,           // open conversation object, or null for a new one
    awaitingCreate: false,   // an ask without conversation_id is in flight
    running: false,
    showArchived: false,
};

const launcher = $("#assistant-launcher");
const windowEl = $("#assistant-window");
const input = $("#aw-input");
const transcript = new Transcript($("#aw-transcript"), {
    onOpenFile: revealFile,
    onOpenImage: openLightbox,
});
const win = createWindow(windowEl, { onVisibility: () => {
    launcher.classList.toggle("is-hidden", win.isOpen);
} });
const socket = new AssistantSocket({
    onFrame: handleFrame,
    onOpen: () => { if (state.current) socket.send({ type: "watch", conversation_id: state.current.id }); },
    onStatus: (s) => windowEl.classList.toggle("is-offline", s === "reconnecting" || s === "closed"),
});

// ----- persistence of small UI choices ---------------------------------------

function storageKey(name) {
    const cfg = assistantConfig.get();
    return `voitta.assistant.${name}.${cfg?.effective?.id ?? 0}`;
}

function remember(name, value) {
    try { localStorage.setItem(storageKey(name), JSON.stringify(value)); } catch { /* storage blocked */ }
}

function recall(name) {
    try { return JSON.parse(localStorage.getItem(storageKey(name))); } catch { return null; }
}

// ----- config (loaded by ./config.js) -----------------------------------------

assistantConfig.subscribe((cfg) => {
    // Off = hidden for users; super-admins keep the launcher so the
    // switched-off state is visible and one click away from being undone.
    launcher.hidden = !cfg || (!cfg.enabled && !cfg.is_admin);
    if (!cfg) return;
    if (launcher.hidden && win.isOpen) win.close();
    $("#aw-view").hidden = !cfg.impersonating;
    const banner = $("#aw-banner");
    banner.hidden = !cfg.impersonating;
    banner.textContent = cfg.impersonating
        ? `Viewing as ${cfg.effective.email} — the assistant searches with their access.`
        : "";
    renderPickers();
    renderSetupNotice();
});

function availableEngines() {
    return (assistantConfig.get()?.engines || []).filter((e) => e.available);
}

function renderSetupNotice() {
    const cfg = assistantConfig.get();
    const setup = $("#aw-setup");
    const off = !!cfg && !cfg.enabled;
    const ready = !off && availableEngines().length > 0;
    setup.hidden = ready || !cfg;
    let reason = "";
    if (off) {
        reason = "The assistant is turned off for everyone — only super-admins see this button.";
    } else if (!ready && cfg) {
        reason = cfg.engines.map((e) => e.reason).filter(Boolean)[0]
            || "The assistant is not configured yet.";
    }
    $("#aw-setup-text").textContent = reason;
    input.disabled = !ready;
    $("#aw-send").disabled = !ready;
}

function fillSelect(select, options, value) {
    select.replaceChildren(...options.map((o) => {
        const opt = document.createElement("option");
        opt.value = o.id;
        opt.textContent = o.label;
        if (o.disabled) opt.disabled = true;
        if (o.title) opt.title = o.title;
        return opt;
    }));
    if (value != null && options.some((o) => o.id === value && !o.disabled)) select.value = value;
}

function renderPickers() {
    const cfg = assistantConfig.get();
    if (!cfg) return;
    const pinned = state.current != null;
    // Compact labels in the closed pickers; full names in the tooltips.
    const engines = cfg.engines.map((e) => ({
        id: e.id, label: e.short, disabled: !e.available, title: e.reason || e.label,
    }));
    fillSelect($("#aw-engine"), engines, recall("engine") || availableEngines()[0]?.id);
    fillSelect(
        $("#aw-model"),
        cfg.models.map((m) => ({ id: m.id, label: m.short, title: m.label })),
        recall("model") || cfg.policy.default_model,
    );
    fillSelect(
        $("#aw-effort"),
        cfg.efforts.map((e) => ({ id: e, label: e, title: `Reasoning effort: ${e}` })),
        recall("effort") || cfg.policy.default_effort,
    );
    $("#aw-engine").hidden = pinned || engines.length < 2;
    $("#aw-model").hidden = pinned;
    const label = $("#aw-pinned");
    label.hidden = !pinned;
    if (pinned) {
        const engine = cfg.engines.find((e) => e.id === state.current.engine);
        const model = cfg.models.find((m) => m.id === state.current.model);
        label.textContent = `${model?.short || state.current.model} · ${engine?.short || state.current.engine}`;
        label.title = `This conversation runs on ${model?.label || state.current.model} `
            + `via ${engine?.label || state.current.engine}`;
    }
}

// ----- conversations ---------------------------------------------------------

async function loadConversations() {
    try {
        state.conversations = await api.assistantConversations(state.view, state.showArchived);
    } catch (err) {
        state.conversations = [];
        transcript.notice(`Could not load conversations: ${err.message}`, "error");
    }
    renderList();
}

function renderList() {
    const ul = $("#aw-list-items");
    ul.replaceChildren();
    if (!state.conversations.length) {
        const li = document.createElement("li");
        li.className = "aw-list-empty";
        li.textContent = "No conversations yet.";
        ul.append(li);
        return;
    }
    for (const c of state.conversations) {
        const li = document.createElement("li");
        li.className = "aw-list-item";
        li.classList.toggle("is-current", state.current?.id === c.id);
        li.classList.toggle("is-archived", c.archived);
        const open = document.createElement("button");
        open.type = "button";
        open.className = "aw-list-open";
        const title = document.createElement("span");
        title.className = "aw-list-title";
        title.textContent = c.title || "Untitled";
        const when = document.createElement("span");
        when.className = "aw-list-when";
        when.textContent = new Date(c.updated_at * 1000).toLocaleString();
        open.append(title, when);
        open.addEventListener("click", () => { openConversation(c.id); toggleList(false); });
        li.append(open, listAction("✎", "Rename", () => renameConversation(c)),
            listAction(c.archived ? "↺" : "⌫", c.archived ? "Unarchive" : "Archive",
                () => archiveConversation(c, !c.archived)),
            listAction("🗑", "Delete", () => deleteConversation(c)));
        ul.append(li);
    }
}

function listAction(glyph, label, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "aw-list-action";
    b.textContent = glyph;
    b.title = label;
    b.setAttribute("aria-label", label);
    b.addEventListener("click", onClick);
    return b;
}

async function renameConversation(c) {
    const title = prompt("Rename conversation", c.title || "");
    if (title == null) return;
    try {
        const updated = await api.assistantUpdateConversation(c.id, { title });
        upsertConversation(updated);
    } catch (err) { alert(err.message); }
}

async function archiveConversation(c, archived) {
    try {
        await api.assistantUpdateConversation(c.id, { archived });
        if (archived && state.current?.id === c.id) newConversation();
        await loadConversations();
    } catch (err) { alert(err.message); }
}

async function deleteConversation(c) {
    if (!confirm(`Delete “${c.title || "Untitled"}”? This cannot be undone.`)) return;
    try {
        await api.assistantDeleteConversation(c.id);
        if (state.current?.id === c.id) newConversation();
        await loadConversations();
    } catch (err) { alert(err.message); }
}

function upsertConversation(conv) {
    const i = state.conversations.findIndex((c) => c.id === conv.id);
    if (i >= 0) state.conversations[i] = conv;
    else state.conversations.unshift(conv);
    if (state.current?.id === conv.id) {
        state.current = conv;
        $("#aw-title").textContent = conv.title || "Assistant";
    }
    renderList();
}

function identityNote(message) {
    const cfg = assistantConfig.get();
    if (!cfg || message.author_user_id == null) return null;
    const notes = [];
    if (message.author_user_id !== cfg.real.id) notes.push(`asked by account #${message.author_user_id}`);
    if (message.acting_user_id != null && message.acting_user_id !== message.author_user_id) {
        notes.push(message.acting_user_id === cfg.effective.id
            ? `as ${cfg.effective.email}` : `as account #${message.acting_user_id}`);
    }
    return notes.join(" ") || null;
}

async function openConversation(id) {
    let data;
    try {
        data = await api.assistantConversation(id);
    } catch {
        remember(`last-${state.view}`, null);
        newConversation();
        return;
    }
    state.current = data.conversation;
    state.running = false;
    state.awaitingCreate = false;
    remember(`last-${state.view}`, id);
    $("#aw-title").textContent = data.conversation.title || "Assistant";
    await transcript.renderStored(data.messages, { identityNote });
    renderPickers();
    renderRunning();
    renderList();
    socket.send({ type: "watch", conversation_id: id });
}

function newConversation() {
    state.current = null;
    state.running = false;
    state.awaitingCreate = false;
    remember(`last-${state.view}`, null);
    $("#aw-title").textContent = "New conversation";
    transcript.clear();
    transcript.notice(
        "Ask about your documents — or about sync health: why a file is missing, "
        + "which folders failed to sync, what the job queue is doing.",
        "muted",
    );
    renderPickers();
    renderRunning();
    renderList();
    input.focus();
}

async function setView(view) {
    if (state.view === view) return;
    state.view = view;
    remember("view", view);
    for (const b of document.querySelectorAll("#aw-view button")) {
        b.classList.toggle("is-active", b.dataset.view === view);
    }
    await loadConversations();
    const last = recall(`last-${view}`);
    if (last) await openConversation(last);
    else newConversation();
}

// ----- sending ---------------------------------------------------------------

function uiContext() {
    const folderId = getSelectedFolderId();
    if (folderId == null) return null;
    const ctx = { folder_id: folderId };
    const folder = folders.get().find((f) => f.id === folderId);
    if (folder) ctx.folder_name = folder.display_name;
    const fileId = getSelectedFileId();
    if (fileId != null) {
        ctx.file_id = fileId;
        const file = files.get().find((f) => f.id === fileId);
        if (file) ctx.file_path = file.rel_path;
    }
    return ctx;
}

function send() {
    const text = input.value.trim();
    if (!text || state.running) return;
    const frame = { type: "ask", text, effort: $("#aw-effort").value, ui_context: uiContext() };
    if (state.current) {
        frame.conversation_id = state.current.id;
    } else {
        frame.view = state.view;
        frame.engine = $("#aw-engine").value;
        frame.model = $("#aw-model").value;
        state.awaitingCreate = true;
        remember("engine", frame.engine);
        remember("model", frame.model);
    }
    remember("effort", frame.effort);
    input.value = "";
    autosize();
    if (!state.current) transcript.clear();
    transcript.appendUser(text);
    transcript.beginTurn();
    state.running = true;
    renderRunning();
    socket.send(frame);
}

function stop() {
    if (state.current && state.running) socket.send({ type: "stop", conversation_id: state.current.id });
}

function renderRunning() {
    $("#aw-send").hidden = state.running;
    $("#aw-stop").hidden = !state.running;
    windowEl.classList.toggle("is-running", state.running);
}

// ----- incoming frames ------------------------------------------------------------

function forCurrent(frame) {
    if (frame.conversation_id == null) return true;
    if (state.current) return frame.conversation_id === state.current.id;
    return state.awaitingCreate;
}

async function handleFrame(frame) {
    switch (frame.type) {
    case "conversation":
        if (!state.current && state.awaitingCreate) {
            state.current = frame.conversation;
            state.awaitingCreate = false;
            remember(`last-${state.view}`, frame.conversation.id);
            renderPickers();
        }
        upsertConversation(frame.conversation);
        return;
    case "error":
        if (state.running || state.awaitingCreate) {
            state.running = false;
            state.awaitingCreate = false;
            renderRunning();
            transcript.endTurn({ status: "error", error: frame.message });
        } else {
            transcript.notice(frame.message, "error");
        }
        return;
    }
    if (!forCurrent(frame)) return;
    switch (frame.type) {
    case "turn_start":
        if (!state.running) {
            state.running = true;
            renderRunning();
            transcript.beginTurn();
        }
        break;
    case "phase": transcript.phase(frame.phase); break;
    case "thinking_delta": transcript.thinkingDelta(frame.seg, frame.text); break;
    case "text_delta": transcript.textDelta(frame.seg, frame.text); break;
    case "text": await transcript.textFinal(frame.seg, frame.text); break;
    case "tool_start": transcript.toolStart(frame.id, frame.name, frame.input); break;
    case "tool_end": transcript.toolEnd(frame.id, frame.is_error, frame.summary, frame.image_ids); break;
    case "turn_end":
        state.running = false;
        renderRunning();
        if (state.current) {
            // The stored transcript is authoritative (it includes a notice
            // row for stopped/failed turns); the live view was a preview.
            await openConversation(state.current.id);
        } else {
            transcript.endTurn(frame);
        }
        loadConversations();
        break;
    default:
        break;
    }
}

// ----- file citations + images -------------------------------------------------

function revealFile(fileId) {
    const file = files.get().find((f) => f.id === fileId);
    if (!file) {
        transcript.notice("That file isn't in your current view.", "muted");
        return;
    }
    const parts = file.rel_path.split("/");
    parts.pop();
    let dir = "";
    expand(file.folder_id, "");
    for (const p of parts) {
        dir = dir ? `${dir}/${p}` : p;
        expand(file.folder_id, dir);
    }
    selectNode(file.folder_id, parts.join("/"), file.id);
}

function openLightbox(imageId) {
    const box = $("#aw-lightbox");
    box.querySelector("img").src = `/api/images/${imageId}`;
    box.hidden = false;
}

// ----- wiring ------------------------------------------------------------------

function toggleList(force) {
    const list = $("#aw-list");
    list.hidden = force === undefined ? !list.hidden : !force;
    if (!list.hidden) loadConversations();
}

function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

async function openWindow() {
    win.open();
    win.restore();
    socket.connect();
    const cfg = assistantConfig.get();
    if (cfg?.impersonating) {
        state.view = recall("view") || "theirs";
        for (const b of document.querySelectorAll("#aw-view button")) {
            b.classList.toggle("is-active", b.dataset.view === state.view);
        }
    }
    await loadConversations();
    const last = recall(`last-${state.view}`);
    if (state.current) return;
    if (last) await openConversation(last);
    else newConversation();
}

launcher.addEventListener("click", openWindow);
$("#aw-close").addEventListener("click", () => win.close());
$("#aw-new").addEventListener("click", () => { newConversation(); toggleList(false); });
$("#aw-history").addEventListener("click", () => toggleList());
$("#aw-show-archived").addEventListener("change", (e) => {
    state.showArchived = e.target.checked;
    loadConversations();
});
for (const b of document.querySelectorAll("#aw-view button")) {
    b.addEventListener("click", () => setView(b.dataset.view));
}
$("#aw-send").addEventListener("click", send);
$("#aw-stop").addEventListener("click", stop);
$("#aw-setup-open").addEventListener("click", () => openSettings({ section: "assistant" }));
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
    }
});
$("#aw-lightbox").addEventListener("click", () => { $("#aw-lightbox").hidden = true; });
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#aw-lightbox").hidden) $("#aw-lightbox").hidden = true;
});
