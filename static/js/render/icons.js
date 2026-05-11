// Inline SVG icons for the file tree.
//
// Three entry points:
//
//   iconForDirKind(kind)    — Google Workspace doc-stem dirs render
//                              with the matching service icon
//                              (presentation / spreadsheet / document
//                              / drawing / form). Returns null for
//                              ordinary directories so the caller
//                              falls back to the folder glyph.
//   iconForFolder()          — generic folder, used for everything
//                              non-Drive-native.
//   iconForFolderRoot()      — top-level managed folder.
//   iconForFile(rel_path)    — extension-driven file icon.
//
// Icons are 14×14 SVG strings injected via innerHTML on the .glyph
// span. Mono icons use ``currentColor`` so they inherit the row's
// text colour (selected / hovered rows look right with no extra
// CSS). Google brand icons carry their own fills.
//
// Why inline SVG and not a sprite sheet or webfont: the renderer
// updates rows in-place, and innerHTML on a single span is cheap.
// A sprite would need an extra HTTP roundtrip and a different
// caching story for icons that nearly never change.

const _GOOGLE_BRAND = {
    document: "#4285F4",      // Docs blue
    spreadsheet: "#0F9D58",   // Sheets green
    presentation: "#F4B400",  // Slides yellow
    drawing: "#DB4437",       // Drawings red
    form: "#673AB7",          // Forms purple
};

const _svgWrap = (body) =>
    `<svg viewBox="0 0 14 14" width="14" height="14" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">${body}</svg>`;

// ---------------------------------------------------------------------------
// Folders
// ---------------------------------------------------------------------------

const _folderPath =
    `<path fill="currentColor" d="M1.5 3.5a1 1 0 0 1 1-1h3.2l1 1H11.5a1 1 0 0 1 1 1V11a1 1 0 0 1-1 1H2.5a1 1 0 0 1-1-1z"/>`;

export function iconForFolder() {
    return _svgWrap(_folderPath);
}

// Visually distinct root-folder badge (collections / mountpoint feel).
const _folderRootPath =
    `<rect x="2" y="2" width="10" height="10" rx="2" fill="currentColor" opacity="0.18"/>` +
    `<rect x="2" y="2" width="10" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.4"/>`;

export function iconForFolderRoot() {
    return _svgWrap(_folderRootPath);
}

// ---------------------------------------------------------------------------
// Google Workspace doc icons (replace the folder for doc-stem dirs)
// ---------------------------------------------------------------------------

function _googleDocBody(kind) {
    const fill = _GOOGLE_BRAND[kind];
    // Common: white "page" with a corner fold over the brand fill.
    const page =
        `<path fill="${fill}" d="M3 1.5h5.2L11.5 4.8V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1z"/>` +
        `<path fill="#fff" opacity="0.35" d="M8.2 1.5v3a.6.6 0 0 0 .6.6h2.7z"/>`;
    // Kind-specific glyph drawn on the page in white.
    let detail = "";
    if (kind === "document") {
        detail =
            `<rect x="3.6" y="6.4" width="6.2" height="0.7" rx="0.2" fill="#fff"/>` +
            `<rect x="3.6" y="7.8" width="6.2" height="0.7" rx="0.2" fill="#fff"/>` +
            `<rect x="3.6" y="9.2" width="4.4" height="0.7" rx="0.2" fill="#fff"/>`;
    } else if (kind === "spreadsheet") {
        detail =
            `<rect x="3.6" y="6.4" width="6.2" height="3.8" rx="0.3" fill="none" stroke="#fff" stroke-width="0.6"/>` +
            `<line x1="3.6" y1="7.7" x2="9.8" y2="7.7" stroke="#fff" stroke-width="0.6"/>` +
            `<line x1="3.6" y1="9.0" x2="9.8" y2="9.0" stroke="#fff" stroke-width="0.6"/>` +
            `<line x1="6.7" y1="6.4" x2="6.7" y2="10.2" stroke="#fff" stroke-width="0.6"/>`;
    } else if (kind === "presentation") {
        detail =
            `<rect x="3.6" y="6.4" width="6.2" height="3.8" rx="0.4" fill="none" stroke="#fff" stroke-width="0.7"/>` +
            `<path fill="#fff" d="M5.9 7.4l2.7 1.5-2.7 1.5z"/>`;
    } else if (kind === "drawing") {
        detail =
            `<path d="M3.8 9.8 L7 6.6 L8.6 8.2 L5.4 11.4 Z" fill="none" stroke="#fff" stroke-width="0.7"/>` +
            `<circle cx="9.5" cy="5.8" r="0.7" fill="#fff"/>`;
    } else if (kind === "form") {
        detail =
            `<rect x="3.6" y="6.3" width="1.4" height="1.4" rx="0.2" fill="#fff"/>` +
            `<rect x="3.6" y="8.5" width="1.4" height="1.4" rx="0.2" fill="#fff"/>` +
            `<rect x="5.6" y="6.6" width="4.2" height="0.7" rx="0.2" fill="#fff"/>` +
            `<rect x="5.6" y="8.8" width="4.2" height="0.7" rx="0.2" fill="#fff"/>`;
    }
    return page + detail;
}

export function iconForDirKind(kind) {
    if (!kind || !(kind in _GOOGLE_BRAND)) return null;
    return _svgWrap(_googleDocBody(kind));
}

// ---------------------------------------------------------------------------
// File icons — extension-driven
// ---------------------------------------------------------------------------

// Base "page with a corner fold" used by every monochrome file icon.
// Drawn in currentColor + low-opacity fill so the file type glyph
// (overlay) reads clearly on top.
const _fileBase =
    `<path fill="currentColor" opacity="0.18" d="M3 1.5h5.2L11.5 4.8V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1z"/>` +
    `<path fill="none" stroke="currentColor" stroke-width="1" d="M3 1.5h5.2L11.5 4.8V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1z M8.2 1.5v3a.6.6 0 0 0 .6.6h2.7"/>`;

function _fileWith(overlay) {
    return _svgWrap(_fileBase + overlay);
}

const _OVERLAYS = {
    markdown:
        `<text x="3.4" y="11.2" font-family="ui-monospace, Menlo, monospace" font-size="4.2" font-weight="700" fill="currentColor">md</text>`,
    pdf:
        `<text x="3.1" y="11.4" font-family="ui-monospace, Menlo, monospace" font-size="3.8" font-weight="700" fill="#DB4437">PDF</text>`,
    image:
        `<rect x="3.2" y="6.4" width="6.8" height="4.4" rx="0.5" fill="none" stroke="currentColor" stroke-width="0.6"/>` +
        `<circle cx="5.0" cy="7.8" r="0.6" fill="currentColor"/>` +
        `<path d="M3.4 10.4 L5.4 8.6 L7.0 9.8 L8.6 8.0 L9.8 10.4 Z" fill="currentColor" opacity="0.65"/>`,
    spreadsheet:
        `<rect x="3.4" y="6.4" width="6.4" height="4.0" rx="0.3" fill="none" stroke="currentColor" stroke-width="0.6"/>` +
        `<line x1="3.4" y1="7.7" x2="9.8" y2="7.7" stroke="currentColor" stroke-width="0.6"/>` +
        `<line x1="3.4" y1="9.0" x2="9.8" y2="9.0" stroke="currentColor" stroke-width="0.6"/>` +
        `<line x1="6.6" y1="6.4" x2="6.6" y2="10.4" stroke="currentColor" stroke-width="0.6"/>`,
    word:
        `<text x="3.0" y="11.4" font-family="ui-monospace, Menlo, monospace" font-size="3.9" font-weight="700" fill="#2A5599">DOC</text>`,
    powerpoint:
        `<rect x="3.4" y="6.4" width="6.4" height="4.0" rx="0.3" fill="none" stroke="currentColor" stroke-width="0.6"/>` +
        `<path d="M5.6 7.4 L8.4 8.4 L5.6 9.4 Z" fill="currentColor"/>`,
    code:
        `<text x="3.0" y="11.4" font-family="ui-monospace, Menlo, monospace" font-size="4.2" font-weight="700" fill="currentColor">&lt;/&gt;</text>`,
    data:
        `<text x="3.4" y="11.4" font-family="ui-monospace, Menlo, monospace" font-size="4.6" font-weight="700" fill="currentColor">{ }</text>`,
    // Generic CAD (.stl / .obj / .gltf / .glb / .3mf / .dxf / .dwg / .iges)
    // — wireframe iso cube in currentColor.
    cad:
        `<path d="M7 6.0 L9.6 7.2 L9.6 9.6 L7 10.8 L4.4 9.6 L4.4 7.2 Z" fill="none" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"/>` +
        `<path d="M7 6.0 L7 8.4 L4.4 9.6 M7 8.4 L9.6 9.6" fill="none" stroke="currentColor" stroke-width="0.7"/>`,
    // STEP / STP — filled teal cube with white wireframe (industrial
    // "blueprint" feel). No formal STEP brand colour exists; teal
    // distinguishes from Docs blue without colliding with FreeCAD red.
    step:
        `<path fill="#0E7C7B" d="M7 5.8 L9.8 7.0 V9.6 L7 10.9 L4.2 9.6 V7.0 Z"/>` +
        `<path fill="none" stroke="#fff" stroke-width="0.6" stroke-linejoin="round" d="M4.2 7.0 L7 8.3 L9.8 7.0 M7 8.3 V10.9"/>`,
    // FCStd — FreeCAD red-orange filled cube with a white "F" mark
    // (the FreeCAD logo's defining colour, simplified to read at 14px).
    freecad:
        `<path fill="#CB333B" d="M7 5.4 L10.4 7.0 V10.0 L7 11.6 L3.6 10.0 V7.0 Z"/>` +
        `<path fill="#fff" d="M5.8 7.4 H8.6 V8.1 H6.6 V8.9 H8.2 V9.6 H6.6 V10.6 H5.8 Z"/>`,
    text:
        `<line x1="3.6" y1="6.8" x2="9.6" y2="6.8" stroke="currentColor" stroke-width="0.7"/>` +
        `<line x1="3.6" y1="8.2" x2="9.6" y2="8.2" stroke="currentColor" stroke-width="0.7"/>` +
        `<line x1="3.6" y1="9.6" x2="7.4" y2="9.6" stroke="currentColor" stroke-width="0.7"/>`,
    archive:
        `<rect x="3.4" y="6.4" width="6.4" height="4.4" rx="0.4" fill="none" stroke="currentColor" stroke-width="0.6"/>` +
        `<rect x="6.4" y="6.4" width="1.2" height="2.0" fill="currentColor" opacity="0.6"/>`,
    audio:
        `<circle cx="5.6" cy="10.4" r="0.9" fill="currentColor"/>` +
        `<path d="M6.5 10.4 V7.2 L9.6 6.4 V9.4" fill="none" stroke="currentColor" stroke-width="0.8" stroke-linecap="round"/>` +
        `<circle cx="8.7" cy="9.4" r="0.9" fill="currentColor"/>`,
    video:
        `<rect x="3.4" y="6.4" width="6.4" height="4.0" rx="0.4" fill="none" stroke="currentColor" stroke-width="0.6"/>` +
        `<path d="M5.4 7.4 L8.6 8.4 L5.4 9.4 Z" fill="currentColor"/>`,
};

const _EXT_TO_KIND = new Map([
    [".md", "markdown"],
    [".markdown", "markdown"],
    [".pdf", "pdf"],
    [".png", "image"], [".jpg", "image"], [".jpeg", "image"],
    [".gif", "image"], [".webp", "image"], [".svg", "image"],
    [".bmp", "image"], [".tif", "image"], [".tiff", "image"],
    [".heic", "image"], [".heif", "image"], [".avif", "image"],
    [".xlsx", "spreadsheet"], [".xls", "spreadsheet"],
    [".csv", "spreadsheet"], [".tsv", "spreadsheet"],
    [".numbers", "spreadsheet"],
    [".docx", "word"], [".doc", "word"], [".odt", "word"],
    [".pages", "word"], [".rtf", "word"],
    [".pptx", "powerpoint"], [".ppt", "powerpoint"],
    [".odp", "powerpoint"], [".key", "powerpoint"],
    [".py", "code"], [".js", "code"], [".mjs", "code"], [".cjs", "code"],
    [".ts", "code"], [".tsx", "code"], [".jsx", "code"],
    [".go", "code"], [".rs", "code"], [".java", "code"], [".kt", "code"],
    [".swift", "code"], [".c", "code"], [".h", "code"],
    [".cpp", "code"], [".hpp", "code"], [".cc", "code"], [".cxx", "code"],
    [".rb", "code"], [".php", "code"], [".sh", "code"], [".bash", "code"],
    [".zsh", "code"], [".pl", "code"], [".lua", "code"], [".scala", "code"],
    [".dart", "code"], [".r", "code"], [".m", "code"], [".sql", "code"],
    [".html", "code"], [".htm", "code"], [".css", "code"], [".scss", "code"],
    [".vue", "code"], [".elm", "code"], [".clj", "code"], [".ex", "code"],
    [".exs", "code"], [".erl", "code"], [".hs", "code"],
    [".json", "data"], [".jsonl", "data"], [".ndjson", "data"],
    [".yaml", "data"], [".yml", "data"], [".toml", "data"], [".xml", "data"],
    [".ini", "data"], [".cfg", "data"], [".conf", "data"], [".env", "data"],
    [".txt", "text"], [".log", "text"], [".text", "text"],
    [".step", "step"], [".stp", "step"], [".iges", "step"], [".igs", "step"],
    [".fcstd", "freecad"],
    [".stl", "cad"], [".obj", "cad"],
    [".glb", "cad"], [".gltf", "cad"], [".3mf", "cad"],
    [".dxf", "cad"], [".dwg", "cad"],
    [".zip", "archive"], [".tar", "archive"], [".tgz", "archive"],
    [".gz", "archive"], [".bz2", "archive"], [".xz", "archive"],
    [".7z", "archive"], [".rar", "archive"], [".zst", "archive"],
    [".mp3", "audio"], [".m4a", "audio"], [".wav", "audio"], [".flac", "audio"],
    [".ogg", "audio"], [".aac", "audio"], [".opus", "audio"], [".wma", "audio"],
    [".mp4", "video"], [".mov", "video"], [".avi", "video"], [".mkv", "video"],
    [".webm", "video"], [".wmv", "video"], [".m4v", "video"],
]);

export function iconForFile(rel_path) {
    if (!rel_path) return _fileWith("");
    // Handle compound suffixes (tar.gz / tar.bz2 → archive) before
    // falling through to single-extension match.
    const lower = rel_path.toLowerCase();
    if (lower.endsWith(".tar.gz") || lower.endsWith(".tar.bz2") || lower.endsWith(".tar.xz")) {
        return _fileWith(_OVERLAYS.archive);
    }
    const dot = lower.lastIndexOf(".");
    const ext = dot >= 0 ? lower.slice(dot) : "";
    const kind = _EXT_TO_KIND.get(ext);
    if (kind && _OVERLAYS[kind]) return _fileWith(_OVERLAYS[kind]);
    return _fileWith("");
}
