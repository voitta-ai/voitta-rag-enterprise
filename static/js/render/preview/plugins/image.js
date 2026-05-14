// Preview plugin: raster and vector images.
// Renders a single <img> that fills the preview body width.

import { registerPlugin } from "../index.js";

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"]);

const plugin = {
    canPreview(file) {
        const ext = _ext(file.rel_path);
        return IMAGE_EXTS.has(ext);
    },

    mount(container, file) {
        container.classList.add("preview-image");
        const img = document.createElement("img");
        img.className = "preview-image-el";
        img.alt = file.rel_path.split("/").pop();
        img.src = `/api/files/${file.id}/raw`;
        img.onerror = () => {
            container.innerHTML = '<p class="preview-error">Could not load image.</p>';
        };
        container.append(img);
    },

    unmount(container) {
        container.classList.remove("preview-image");
        container.innerHTML = "";
    },
};

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
