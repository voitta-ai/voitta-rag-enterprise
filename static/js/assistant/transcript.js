// Renders a conversation: the stored transcript (REST) and the live turn
// (WS events) into the SAME DOM shapes, so reloading the stored version
// after a turn is visually seamless.
//
// Everything model-produced goes through the sanitising markdown renderer;
// everything else (user text, tool arguments/results, errors) is set as
// text — never parsed as HTML.

import { enhance, renderMarkdown } from "../render/markdown.js";

const TOOL_LABELS = {
    list_folders: "Folders",
    search: "Search",
    search_images: "Image search",
    get_chunk_range: "Read chunks",
    get_file: "Read file",
    get_chunk_images: "Chunk figures",
    get_image: "Image",
    list_page_images: "Pages",
    get_page_image: "Page",
    get_page_layout: "Page layout",
    resolve_url: "Resolve URL",
    sync_overview: "Sync overview",
    folder_sync_detail: "Folder sync detail",
    file_problems: "File problems",
    recent_jobs: "Job queue",
};
const RESULT_PREVIEW_CHARS = 6000;
// Re-render streamed markdown at most this often (ms).
const STREAM_RENDER_MS = 80;
const NEAR_BOTTOM_PX = 48;

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

function argsSummary(name, input) {
    if (!input || typeof input !== "object") return "";
    if (typeof input.query === "string") return `“${input.query}”`;
    if (typeof input.prefix === "string" && input.prefix) return input.prefix;
    const parts = Object.entries(input)
        .filter(([, v]) => v !== null && v !== undefined && v !== "")
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
    return parts.join(" ");
}

function prettyResult(text) {
    let out = text;
    try { out = JSON.stringify(JSON.parse(text), null, 2); } catch { /* not JSON */ }
    return out.length > RESULT_PREVIEW_CHARS
        ? `${out.slice(0, RESULT_PREVIEW_CHARS)}\n… (${out.length - RESULT_PREVIEW_CHARS} more characters)`
        : out;
}

export class Transcript {
    constructor(container, { onOpenFile, onOpenImage }) {
        this._root = container;
        this._onOpenImage = onOpenImage;
        this._live = null;
        // Follow new content only while the reader is at the bottom; decided
        // on scroll (before content grows), not after appending.
        this._stick = true;
        container.addEventListener("scroll", () => { this._stick = this._nearBottom(); });
        container.addEventListener("click", (e) => {
            const link = e.target.closest("a[href^='#voitta-file-']");
            if (link) {
                e.preventDefault();
                const id = Number(link.getAttribute("href").slice("#voitta-file-".length));
                if (Number.isFinite(id)) onOpenFile(id);
                return;
            }
            const img = e.target.closest("img[data-image-id]");
            if (img) onOpenImage(Number(img.dataset.imageId));
        });
    }

    clear() {
        this._live = null;
        this._root.replaceChildren();
    }

    // ----- stored transcript -------------------------------------------

    async renderStored(messages, { identityNote } = {}) {
        const frag = document.createDocumentFragment();
        const cards = new Map();  // tool_use id → card, filled by later tool rows
        let assistant = null;
        for (const m of messages) {
            if (m.role === "user") {
                assistant = null;
                frag.append(this._userBubble(
                    m.content.filter((b) => b.type === "text").map((b) => b.text).join("\n"),
                    identityNote?.(m),
                ));
            } else if (m.role === "assistant") {
                if (!assistant) {
                    assistant = el("div", "as-msg as-assistant");
                    frag.append(assistant);
                }
                for (const b of m.content) {
                    if (b.type === "thinking" && b.thinking) {
                        assistant.append(this._thinking(b.thinking));
                    } else if (b.type === "text") {
                        const div = el("div", "as-text");
                        div.append(await renderMarkdown(b.text));
                        assistant.append(div);
                    } else if (b.type === "tool_use") {
                        const card = this._toolCard(b.name, b.input);
                        cards.set(b.id, card);
                        assistant.append(card.root);
                    }
                }
            } else if (m.role === "notice") {
                assistant = null;
                for (const b of m.content) {
                    frag.append(el("div", `as-notice as-notice-${b.kind === "error" ? "error" : "muted"}`, b.text));
                }
            } else if (m.role === "tool") {
                for (const r of m.content) {
                    const card = cards.get(r.tool_use_id);
                    if (!card) continue;
                    const text = (r.content || []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
                    const images = (r.content || []).filter((c) => c.type === "image_ref").map((c) => c.image_id);
                    this._finishCard(card, r.is_error, r.is_error ? text : "", images, text);
                }
            }
        }
        this._root.replaceChildren(frag);
        await enhance(this._root);
        this.scrollToBottom();
    }

    // ----- live turn ----------------------------------------------------

    appendUser(text) {
        this._append(this._userBubble(text));
    }

    beginTurn() {
        const root = el("div", "as-msg as-assistant as-live");
        const status = el("div", "as-status");
        status.append(el("span", "as-spinner"), el("span", "as-status-text", "Thinking…"));
        root.append(status);
        this._live = { root, status, segs: new Map(), cards: new Map() };
        this._append(root);
    }

    phase(phase) {
        if (!this._live) this.beginTurn();
        const label = { thinking: "Thinking…", writing: "Writing…", tool: "Using tools…" }[phase];
        if (label) this._live.status.querySelector(".as-status-text").textContent = label;
    }

    thinkingDelta(seg, text) {
        const s = this._seg(seg, "thinking");
        s.buffer += text;
        s.body.textContent = s.buffer;
        this._keepBottom();
    }

    textDelta(seg, text) {
        const s = this._seg(seg, "text");
        s.buffer += text;
        this._scheduleRender(s);
    }

    async textFinal(seg, text) {
        const s = this._seg(seg, "text");
        s.buffer = text;
        s.final = true;
        clearTimeout(s.timer);
        await this._renderSeg(s);
        await enhance(s.node);
    }

    toolStart(id, name, input) {
        if (!this._live) this.beginTurn();
        const card = this._toolCard(name, input, true);
        this._live.cards.set(id, card);
        this._live.root.insertBefore(card.root, this._live.status);
        this._keepBottom();
    }

    toolEnd(id, isError, summary, imageIds) {
        const card = this._live?.cards.get(id);
        if (card) this._finishCard(card, isError, summary, imageIds || [], null);
        this._keepBottom();
    }

    endTurn({ status, error }) {
        if (this._live) {
            this._live.status.remove();
            this._live.root.classList.remove("as-live");
            for (const d of this._live.root.querySelectorAll("details.as-thinking[open]")) d.open = false;
        }
        this._live = null;
        if (status === "interrupted") this.notice("Stopped.", "muted");
        else if (status === "error") this.notice(error || "Something went wrong.", "error");
    }

    notice(text, kind = "muted") {
        this._append(el("div", `as-notice as-notice-${kind}`, text));
    }

    scrollToBottom() {
        this._root.scrollTop = this._root.scrollHeight;
        this._stick = true;
    }

    // ----- pieces ---------------------------------------------------------

    _userBubble(text, note) {
        const wrap = el("div", "as-msg as-user");
        wrap.append(el("div", "as-bubble", text));
        if (note) wrap.append(el("div", "as-meta", note));
        return wrap;
    }

    _thinking(text) {
        const d = el("details", "as-thinking");
        d.append(el("summary", null, "Thinking"), el("div", "as-thinking-text", text));
        return d;
    }

    _toolCard(name, input, pending = false) {
        const root = el("details", "as-tool");
        const summary = el("summary");
        const status = el("span", "as-tool-status", pending ? "…" : "");
        summary.append(
            el("span", "as-tool-name", TOOL_LABELS[name] || name),
            el("span", "as-tool-args", argsSummary(name, input)),
            status,
        );
        const body = el("div", "as-tool-body");
        const images = el("div", "as-tool-images");
        root.append(summary, body);
        const wrap = el("div", "as-tool-wrap");
        wrap.append(root, images);
        return { root: wrap, details: root, status, body, images };
    }

    _finishCard(card, isError, summary, imageIds, fullText) {
        card.details.classList.toggle("is-error", !!isError);
        card.status.textContent = isError ? "failed" : (summary || "done");
        if (fullText) card.body.append(el("pre", "as-tool-result", prettyResult(fullText)));
        else if (isError && summary) card.body.append(el("pre", "as-tool-result", summary));
        for (const id of imageIds) {
            const img = el("img");
            img.src = `/api/images/${id}`;
            img.alt = `Image ${id}`;
            img.loading = "lazy";
            img.dataset.imageId = String(id);
            card.images.append(img);
        }
    }

    _seg(seg, kind) {
        if (!this._live) this.beginTurn();
        let s = this._live.segs.get(seg);
        if (!s) {
            if (kind === "thinking") {
                const d = this._thinking("");
                d.open = true;
                s = { kind, node: d, body: d.querySelector(".as-thinking-text"), buffer: "" };
            } else {
                s = { kind, node: el("div", "as-text"), buffer: "", version: 0, timer: null };
            }
            this._live.segs.set(seg, s);
            this._live.root.insertBefore(s.node, this._live.status);
        }
        return s;
    }

    _scheduleRender(s) {
        if (s.timer) return;
        s.timer = setTimeout(() => {
            s.timer = null;
            this._renderSeg(s);
        }, STREAM_RENDER_MS);
    }

    async _renderSeg(s) {
        const version = ++s.version;
        const frag = await renderMarkdown(s.buffer);
        if (version !== s.version) return;  // a newer render superseded this one
        s.node.replaceChildren(frag);
        this._keepBottom();
    }

    _append(node) {
        this._root.append(node);
        this._keepBottom();
    }

    _nearBottom() {
        const r = this._root;
        return r.scrollHeight - r.scrollTop - r.clientHeight < NEAR_BOTTOM_PX;
    }

    _keepBottom() {
        if (this._stick) this.scrollToBottom();
    }
}
