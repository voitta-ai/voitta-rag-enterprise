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
    const codeBlocks = blocks.filter((c) => !c.classList.contains("language-mermaid"));
    for (const c of blocks) c.dataset.enhanced = "1";
    await Promise.all([_highlight(codeBlocks), _diagrams(mermaidBlocks)]);
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
