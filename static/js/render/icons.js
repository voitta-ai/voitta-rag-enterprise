// Icon registry — file, folder, and data-source icons for the tree.
//
// Backed by vendored SVGs under /static/icons/ (see ../../icons/README.md
// for provenance + license info). Returns a path string the caller drops
// into an <img> ``src`` attribute. No inline SVG, no innerHTML churn.
//
// Three lookups:
//
//   iconForFile(rel_path)         — by extension
//   iconForDirKind(kind)          — Google-suite doc-stem dirs
//   iconForSource(syncSourceKind) — data-source badge for top-level rows
//   iconForFolder()               — generic neutral folder (subdirs)
//
// All paths are absolute under /static/icons/ so the browser can cache
// them aggressively and re-renders only swap the src — never the DOM
// node.

const PATH = "/static/icons";
const M_FILE = `${PATH}/material/file`;
const M_FOLDER = `${PATH}/material/folder`;
const M_SRC = `${PATH}/material/source`;
const SIMPLE = `${PATH}/simple`;

// GitHub's logo is a monochrome octocat — needs a dark fill on a
// light background and a light fill on a dark background. We ship
// both prebaked variants and pick at lookup time from the body's
// ``data-theme`` attribute. Reading the live DOM means the function
// stays correct after a runtime theme toggle without any subscriber
// plumbing — the icon resolver is called on every reconcile pass.
function _githubIcon() {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark"
        || document.body.getAttribute("data-theme") === "dark";
    return `${SIMPLE}/${isDark ? "github-dark" : "github-light"}.svg`;
}

// ---------------------------------------------------------------------------
// Folder icons
// ---------------------------------------------------------------------------

// Generic folder for ordinary subdirs (not top-level, not a Google
// doc-stem). PKief's ``folder-base`` is the neutral grey variant —
// matches the "this folder doesn't have a special meaning" intent.
export function iconForFolder() {
    return `${M_FOLDER}/folder-base.svg`;
}

// Top-level folder ROW source-type icons. The badge identifies where
// the folder's content comes from. Anything not yet in the registry
// falls back to a neutral upload folder so the row never goes naked.
//
//   regular         — local filesystem upload, no sync source
//   github_public   — GitHub mirror, no credentials
//   github_private  — GitHub mirror, SSH/token credentials
//   google_drive    — Drive folder sync
//
// Future connectors (sharepoint, dropbox, s3, azure_data_lake, gcs):
// add the kind to ``sync_source_kind`` server-side and a row here.
// Simple Icons ships googledrive / dropbox / googlecloud today;
// Microsoft + AWS were dropped by Simple Icons in 2024 (brand policy)
// so SharePoint / Azure / S3 stay TODO until we pick a replacement set.
// Source-row resolvers. GitHub uses the proper Octocat brand mark
// in light/dark variants; everything else uses a brand-coloured
// vendor SVG (Google Drive's three-colour mark, etc.). The folder-
// upload (Material) is the only Material-folder icon kept here —
// it visually means "data lives in this folder, was uploaded".
function _sourceIcon(kind) {
    switch (kind) {
        case "github_public":
        case "github_private":
            return _githubIcon();  // brand octocat, theme-aware
        case "google_drive":
            return `${SIMPLE}/googledrive.svg`;
        case "dropbox":
            return `${SIMPLE}/dropbox.svg`;
        case "gcs":
            return `${SIMPLE}/googlecloud.svg`;
        // TODO: vendor sharepoint / amazons3 / microsoftazure when
        //       those connectors land. Simple Icons dropped Microsoft
        //       and AWS in 2024 — pick a replacement set then.
        case "sharepoint":
        case "s3":
        case "azure_data_lake":
        case "regular":
        default:
            return `${M_FOLDER}/folder-upload.svg`;
    }
}

export function iconForSource(syncSourceKind) {
    return _sourceIcon(syncSourceKind);
}

// Private GitHub repos need a lock badge over the octocat — the
// caller layers a separate <img> on top via the .source-lock CSS
// class. This flag tells the caller whether to render it.
export function sourceNeedsLockBadge(syncSourceKind) {
    return syncSourceKind === "github_private";
}

export function lockBadgeIcon() {
    return `${M_SRC}/lock.svg`;
}

// ---------------------------------------------------------------------------
// Google Workspace doc-stem dirs
// ---------------------------------------------------------------------------

const _GOOGLE_DIR_ICONS = {
    document: `${SIMPLE}/googledocs.svg`,
    spreadsheet: `${SIMPLE}/googlesheets.svg`,
    presentation: `${SIMPLE}/googleslides.svg`,
    // Simple Icons no longer ships Google Drawings — fall through to
    // the generic Docs icon so the dir is at least visibly "Google".
    drawing: `${SIMPLE}/googledocs.svg`,
    form: `${SIMPLE}/googleforms.svg`,
};

export function iconForDirKind(kind) {
    if (!kind) return null;
    return _GOOGLE_DIR_ICONS[kind] || null;
}

// ---------------------------------------------------------------------------
// File icons
// ---------------------------------------------------------------------------

// Material Icon Theme covers every extension we currently render in
// the demo file tree. Anything not in this map falls back to the
// neutral ``document.svg`` — same look the upstream theme uses for
// "unknown file type" in VS Code.
const _EXT_TO_FILE_ICON = new Map([
    // Text / markup / data
    [".md", "markdown"], [".markdown", "markdown"],
    [".pdf", "pdf"],
    [".txt", "document"], [".text", "document"], [".rtf", "document"],
    [".log", "log"],
    [".json", "json"], [".jsonl", "json"], [".ndjson", "json"],
    [".yaml", "yaml"], [".yml", "yaml"],
    [".toml", "toml"],
    [".xml", "xml"], [".plist", "xml"],
    // Office
    [".doc", "word"], [".docx", "word"], [".odt", "word"], [".pages", "word"],
    [".xls", "table"], [".xlsx", "table"], [".ods", "table"], [".csv", "table"], [".tsv", "table"],
    [".ppt", "powerpoint"], [".pptx", "powerpoint"], [".odp", "powerpoint"], [".key", "powerpoint"],
    // Images
    [".png", "image"], [".jpg", "image"], [".jpeg", "image"],
    [".gif", "image"], [".webp", "image"], [".bmp", "image"],
    [".tif", "image"], [".tiff", "image"],
    [".heic", "image"], [".heif", "image"], [".avif", "image"],
    [".svg", "svg"],
    // Audio / video
    [".mp3", "audio"], [".m4a", "audio"], [".wav", "audio"], [".flac", "audio"],
    [".ogg", "audio"], [".aac", "audio"], [".opus", "audio"], [".wma", "audio"],
    [".mp4", "video"], [".mov", "video"], [".avi", "video"], [".mkv", "video"],
    [".webm", "video"], [".wmv", "video"], [".m4v", "video"],
    // Archives — Material uses ``zip.svg`` for the whole family
    [".zip", "zip"], [".tar", "zip"], [".gz", "zip"], [".tgz", "zip"],
    [".bz2", "zip"], [".xz", "zip"], [".7z", "zip"], [".rar", "zip"], [".zst", "zip"],
    // Code
    [".py", "python"], [".pyi", "python"], [".pyx", "python"],
    [".js", "javascript"], [".mjs", "javascript"], [".cjs", "javascript"],
    [".ts", "typescript"],
    [".jsx", "react"], [".tsx", "react"],
    [".html", "html"], [".htm", "html"],
    [".css", "css"], [".scss", "css"], [".sass", "css"],
    [".sh", "console"], [".bash", "console"], [".zsh", "console"], [".fish", "console"],
    // CAD
    [".step", "3d"], [".stp", "3d"], [".iges", "3d"], [".igs", "3d"],
    [".stl", "3d"], [".obj", "3d"], [".glb", "3d"], [".gltf", "3d"], [".3mf", "3d"],
    // .FCStd → Simple Icons FreeCAD brand mark (handled below; not in
    // the Material set).
]);

export function iconForFile(rel_path) {
    if (!rel_path) return `${M_FILE}/document.svg`;
    const lower = rel_path.toLowerCase();
    // FreeCAD: brand-faithful Simple Icons logo. Special-cased because
    // it lives in a different vendor dir.
    if (lower.endsWith(".fcstd")) return `${SIMPLE}/freecad.svg`;
    // Compound suffixes — collapse to archive before single-ext lookup.
    if (lower.endsWith(".tar.gz") || lower.endsWith(".tar.bz2") || lower.endsWith(".tar.xz")) {
        return `${M_FILE}/zip.svg`;
    }
    const dot = lower.lastIndexOf(".");
    const ext = dot >= 0 ? lower.slice(dot) : "";
    const slug = _EXT_TO_FILE_ICON.get(ext);
    if (slug) return `${M_FILE}/${slug}.svg`;
    return `${M_FILE}/document.svg`;
}
