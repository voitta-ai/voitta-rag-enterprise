// Preview plugin: RFC-822 emails (.eml).
//
// Backend parses the file with Python's stdlib ``email`` module and
// returns structured headers + body parts + attachment metadata.
//
// HTML bodies render inside a ``<iframe sandbox="">`` — no flags = no
// scripts, no same-origin, no form submission, no top-nav. That makes
// it safe to drop in arbitrary mail HTML (which is the most hostile
// HTML on the public internet) without a sanitizer dependency.

import { api } from "../../../api.js";
import { registerPlugin } from "../index.js";

let _abortCtrl = null;

const plugin = {
    canPreview(file) {
        return _ext(file.rel_path) === ".eml";
    },

    async mount(container, file) {
        container.classList.add("preview-email");
        container.innerHTML = '<p class="preview-loading">Parsing email…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        try {
            const data = await api.fileEmail(file.id);
            if (signal.aborted) return;
            container.innerHTML = "";
            container.append(_renderEmail(data));
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">${err.message}</p>`;
        }
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        container.classList.remove("preview-email");
        container.innerHTML = "";
    },
};

function _renderEmail(data) {
    const root = document.createElement("div");
    root.className = "email-view";

    // Header card
    const header = document.createElement("div");
    header.className = "email-headers";
    const subject = document.createElement("h3");
    subject.className = "email-subject";
    subject.textContent = data.subject || "(no subject)";
    header.append(subject);

    const fields = document.createElement("dl");
    fields.className = "email-fields";
    _addField(fields, "From", data.from_addr);
    _addField(fields, "To", data.to);
    _addField(fields, "Cc", data.cc);
    _addField(fields, "Bcc", data.bcc);
    _addField(fields, "Reply-To", data.reply_to);
    _addField(fields, "Date", data.date);
    header.append(fields);
    root.append(header);

    // Attachments
    if (data.attachments?.length) {
        const att = document.createElement("div");
        att.className = "email-attachments";
        const label = document.createElement("p");
        label.className = "email-att-label";
        label.textContent = `Attachments (${data.attachments.length})`;
        att.append(label);
        const ul = document.createElement("ul");
        ul.className = "email-att-list";
        for (const a of data.attachments) {
            const li = document.createElement("li");
            const name = document.createElement("span");
            name.className = "email-att-name";
            name.textContent = a.filename;
            const meta = document.createElement("span");
            meta.className = "email-att-meta";
            meta.textContent = ` · ${a.content_type} · ${_humanBytes(a.size)}`;
            li.append(name, meta);
            ul.append(li);
        }
        att.append(ul);
        root.append(att);
    }

    // Body. Prefer HTML rendered in a sandboxed iframe; fall back to
    // plain text in a <pre>. The iframe sizes itself to its content
    // via a one-shot postMessage from a tiny inline script — that
    // inline script is what the sandbox attribute is there to disarm
    // for hostile HTML, so we do the sizing from the parent instead
    // via a ResizeObserver on the iframe's documentElement (after the
    // initial paint), and fall back to a fixed max-height otherwise.
    const body = document.createElement("div");
    body.className = "email-body";
    if (data.body_html) {
        const iframe = document.createElement("iframe");
        iframe.className = "email-html-frame";
        // Empty sandbox = maximum restriction. allow-same-origin would
        // be required to read the iframe's body height, but we skip it
        // intentionally — pay a fixed max-height instead of opening the
        // door to email content reaching our cookies.
        iframe.sandbox = "";
        iframe.srcdoc = data.body_html;
        body.append(iframe);
    } else if (data.body_text) {
        const pre = document.createElement("pre");
        pre.className = "email-text";
        pre.textContent = data.body_text;
        body.append(pre);
    } else {
        const empty = document.createElement("p");
        empty.className = "preview-hint";
        empty.textContent = "(empty message body)";
        body.append(empty);
    }
    root.append(body);

    return root;
}

function _addField(dl, label, value) {
    if (!value) return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
}

function _humanBytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
