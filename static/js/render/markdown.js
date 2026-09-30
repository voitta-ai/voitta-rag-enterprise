// Shared, sanitising markdown renderer.
//
// Every piece of markdown the UI renders comes from somewhere we don't
// control: an indexed file (the preview pane) or an LLM whose output can be
// steered by the documents it read (the assistant). ``marked`` passes raw
// HTML straight through, so its output is ALWAYS run through DOMPurify
// before it touches the DOM — never assign ``marked.parse()`` to innerHTML
// directly.
//
// The libraries are vendored under /static/js/vendor (no CDN — the UI must
// work offline and inside the desktop app) and lazy-loaded on first use:
//   * marked + DOMPurify     — every render
//   * highlight.js           — only when ``enhance()`` meets a code block
//   * mermaid (3.5 MB UMD)   — only when ``enhance()`` meets a mermaid block
//
// ``enhance()`` also shows SVG code blocks as the image they describe
// (with a toggle back to the code). The SVG is rendered through an <img>
// from a data: URL — the image context never runs scripts or event
// handlers and never fetches external resources, so a hostile SVG can't do
// anything but draw.

let _core = null;       // Promise<{ marked, purify }>
let _hljs = null;       // Promise<hljs>
let _mermaid = null;    // Promise<mermaid>
let _mermaidSeq = 0;

function _coreLibs() {
    if (!_core) {
        _core = Promise.all([
            import("/static/js/vendor/marked.js"),
            import("/static/js/vendor/purify.js"),
        ]).then(([markedMod, purifyMod]) => {
            const marked = markedMod.marked;
            marked.setOptions({ gfm: true, breaks: false });
            const purify = purifyMod.default;
            // Links open in a new tab and never hand the opener to the
            // target. A hook (not a post-pass) so it also covers links
            // inside content a caller sanitises through ``sanitizeHtml``.
            purify.addHook("afterSanitizeAttributes", (node) => {
                if (node.tagName === "A" && node.hasAttribute("href")) {
                    node.setAttribute("target", "_blank");
                    node.setAttribute("rel", "noopener noreferrer");
                }
            });
            return { marked, purify };
        });
    }
    return _core;
}

// Parse ``text`` as markdown and return a sanitised DocumentFragment.
export async function renderMarkdown(text) {
    const { marked, purify } = await _coreLibs();
    return purify.sanitize(marked.parse(text ?? ""), {
        USE_PROFILES: { html: true },
        RETURN_DOM_FRAGMENT: true,
    });
}

// Parse ``text`` as markdown into a new <article class="..."> appended to
// ``container``. Kept as the preview plugins' entry point.
export async function renderMarkdownInto(container, text, className = "preview-markdown") {
    const article = document.createElement("article");
    article.className = className;
    article.append(await renderMarkdown(text));
    container.append(article);
    return article;
}

// Upgrade rendered markdown in place: syntax-highlight fenced code and turn
// ```mermaid fences into diagrams. Safe to call repeatedly on the same root
// (processed blocks are marked). Failures leave the plain code block.
export async function enhance(root) {
    const blocks = [...root.querySelectorAll("pre > code:not([data-enhanced])")];
    if (!blocks.length) return;
    const mermaidBlocks = blocks.filter((c) => c.classList.contains("language-mermaid"));
    const codeBlocks = blocks.filter((c) => !mermaidBlocks.includes(c));
    // Detect SVG before highlighting: highlight.js adds its own language
    // class to untagged blocks, which would change what _svgSource sees.
    const svgBlocks = codeBlocks
        .map((code) => ({ code, source: _svgSource(code) }))
        .filter((b) => b.source !== null);
    for (const c of blocks) c.dataset.enhanced = "1";
    // Highlight first so the code view behind an SVG preview is coloured too.
    await Promise.all([_highlight(codeBlocks), _diagrams(mermaidBlocks)]);
    for (const { code, source } of svgBlocks) _svgPreview(code, source);
}

// Fenced code that is an SVG document: ```svg, or ```xml / ```html / an
// untagged fence whose content is a single <svg>…</svg> (optionally after an
// XML declaration or comments). Returns the source, or null.
const _SVG_LANGS = ["language-svg", "language-xml", "language-html", "language-plaintext"];
const _SVG_DOC = /^\s*(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*<svg[\s>][\s\S]*<\/svg>\s*$/i;

function _svgSource(code) {
    const tagged = [...code.classList].find((c) => c.startsWith("language-"));
    if (tagged && !_SVG_LANGS.includes(tagged)) return null;
    const text = code.textContent;
    return _SVG_DOC.test(text) ? text.trim() : null;
}

function _svgPreview(code, source) {
    const doc = new DOMParser().parseFromString(source, "image/svg+xml");
    const root = doc.documentElement;
    if (doc.querySelector("parsererror") || root.localName !== "svg") {
        code.parentElement.title = "Not a valid SVG document — shown as code.";
        return;
    }
    // Standalone SVG images must declare the namespace to render.
    if (!root.getAttribute("xmlns")) root.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const markup = new XMLSerializer().serializeToString(root);

    const pre = code.parentElement;
    const figure = document.createElement("figure");
    figure.className = "md-svg";

    const bar = document.createElement("div");
    bar.className = "md-svg-bar";
    const label = document.createElement("span");
    label.className = "md-svg-label";
    label.textContent = "SVG";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "md-svg-btn";
    toggle.textContent = "Code";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "md-svg-btn";
    copy.textContent = "Copy";
    bar.append(label, toggle, copy);

    const stage = document.createElement("div");
    stage.className = "md-svg-stage";
    const img = document.createElement("img");
    img.alt = root.getAttribute("aria-label") || "SVG image";
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
    // An SVG with only a viewBox has no intrinsic size inside <img>.
    if (!root.getAttribute("width") && !root.getAttribute("height")) img.classList.add("md-svg-fluid");
    stage.append(img);

    pre.replaceWith(figure);
    pre.hidden = true;
    figure.append(bar, stage, pre);

    toggle.addEventListener("click", () => {
        const showCode = pre.hidden;
        pre.hidden = !showCode;
        stage.hidden = showCode;
        toggle.textContent = showCode ? "Image" : "Code";
    });
    copy.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(source);
            copy.textContent = "Copied";
        } catch {
            copy.textContent = "Copy failed";
        }
        setTimeout(() => { copy.textContent = "Copy"; }, 1200);
    });
}

async function _highlight(blocks) {
    if (!blocks.length) return;
    try {
        if (!_hljs) {
            _hljs = import("/static/js/vendor/highlight/highlight.min.js").then((m) => m.default);
        }
        const hljs = await _hljs;
        for (const code of blocks) hljs.highlightElement(code);
    } catch (err) {
        console.warn("code highlighting unavailable", err);
    }
}

function _loadMermaid() {
    if (!_mermaid) {
        _mermaid = new Promise((resolve, reject) => {
            const s = document.createElement("script");
            s.src = "/static/js/vendor/mermaid.min.js";
            s.onload = () => {
                const m = globalThis.mermaid;
                // strict: no click handlers / script in diagram source.
                // htmlLabels off: labels render as SVG text, not
                // <foreignObject> HTML, so the SVG profile below can
                // sanitise the output without stripping every label.
                m.initialize({
                    startOnLoad: false,
                    securityLevel: "strict",
                    htmlLabels: false,
                    flowchart: { htmlLabels: false },
                    theme: _isDark() ? "dark" : "default",
                });
                resolve(m);
            };
            s.onerror = () => reject(new Error("mermaid failed to load"));
            document.head.append(s);
        });
    }
    return _mermaid;
}

async function _diagrams(blocks) {
    if (!blocks.length) return;
    let mermaid;
    try {
        mermaid = await _loadMermaid();
    } catch (err) {
        console.warn(err);
        return;
    }
    const { purify } = await _coreLibs();
    for (const code of blocks) {
        const pre = code.parentElement;
        try {
            const { svg } = await mermaid.render(`mmd-${++_mermaidSeq}`, code.textContent);
            const figure = document.createElement("figure");
            figure.className = "md-diagram";
            figure.append(purify.sanitize(svg, {
                USE_PROFILES: { svg: true, svgFilters: true },
                RETURN_DOM_FRAGMENT: true,
            }));
            pre.replaceWith(figure);
        } catch (err) {
            // Invalid diagram source: keep the fenced code so the user
            // still sees what the model wrote.
            pre.classList.add("md-diagram-error");
            pre.title = `Diagram could not be rendered: ${err.message || err}`;
        }
    }
}

function _isDark() {
    return document.documentElement.getAttribute("data-theme") === "dark"
        || document.body.getAttribute("data-theme") === "dark";
}
