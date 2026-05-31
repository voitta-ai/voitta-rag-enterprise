// Shared markdown loader + renderer for preview plugins.
// `marked` is lazy-loaded from a locally-vendored bundle on first call and
// cached at module scope so subsequent plugins reuse it without a second
// fetch. Vendored under /static/js/vendor so the UI has no external-CDN
// dependency (works on locked-down / offline networks).

let _marked = null;

export async function getMarked() {
    if (_marked) return _marked;
    const mod = await import("/static/js/vendor/marked.js");
    _marked = mod.marked;
    _marked.setOptions({ gfm: true, breaks: false });
    return _marked;
}

// Parse `text` as markdown into a new <article> appended to `container`.
// Links get target=_blank so they don't navigate away from the app.
export async function renderMarkdownInto(container, text) {
    const marked = await getMarked();
    const article = document.createElement("article");
    article.className = "preview-markdown";
    article.innerHTML = marked.parse(text);
    for (const a of article.querySelectorAll("a[href]")) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
    }
    container.append(article);
}
