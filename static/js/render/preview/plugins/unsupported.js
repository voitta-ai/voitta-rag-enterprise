// Preview plugin: catch-all fallback.
// Shows file metadata and a "No preview available" message.

import { registerPlugin } from "../index.js";

const plugin = {
    canPreview(_file) {
        return true; // matches everything — must be registered last
    },

    mount(container, file) {
        container.classList.add("preview-unsupported");
        const ext = (() => {
            const dot = file.rel_path.lastIndexOf(".");
            return dot >= 0 ? file.rel_path.slice(dot).toLowerCase() : "(no extension)";
        })();
        container.innerHTML = `
            <p class="preview-hint">No preview available for <strong>${ext}</strong> files.</p>
            <p class="preview-hint">Download the file to open it locally.</p>
        `;
    },

    unmount(container) {
        container.classList.remove("preview-unsupported");
        container.innerHTML = "";
    },
};

registerPlugin(plugin);
