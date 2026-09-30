// Preview plugin: catch-all fallback.
// Shows file metadata and a "No preview available" message.

import { previewMessage, registerPlugin } from "../index.js";

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
        // Built with text nodes: the extension comes from a (possibly synced,
        // attacker-named) file name and must never be parsed as markup.
        const first = previewMessage("No preview available for ", "hint");
        const strong = document.createElement("strong");
        strong.textContent = ext;
        first.append(strong, " files.");
        container.replaceChildren(
            first,
            previewMessage("Download the file to open it locally.", "hint"),
        );
    },

    unmount(container) {
        container.classList.remove("preview-unsupported");
        container.innerHTML = "";
    },
};

registerPlugin(plugin);
