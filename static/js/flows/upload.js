// Multi-file upload flow.
//
// Triggered by the toolbar Upload button (and indirectly by the
// hidden file input). Concurrency-limited (3 in-flight) so a 200-file
// drop doesn't pin the network. Per-file progress lines stay visible
// until everything settles, then auto-clear after 1.5s (5s on
// failure so the user can read the error).

import { api } from "../api.js";
import { getSelectedFolderId, getSelectedRelDir } from "./selection.js";

const $ = (sel) => document.querySelector(sel);

$("#btn-upload").addEventListener("click", () => $("#upload-input").click());

$("#upload-input").addEventListener("change", async (e) => {
    const selected = Array.from(e.target.files);
    if (!selected.length || !getSelectedFolderId()) return;

    const wrap = $("#upload-progress");
    const fill = $("#upload-progress-fill");
    const label = $("#upload-progress-label");
    const list = $("#upload-file-list");

    // Per-file row: name + percent + state class. Bytes-loaded across all
    // files drives the aggregate bar so the user can also see total
    // throughput at a glance. Rows persist on completion and surface the
    // ✓ / × state — see issue #23 (visible per-file completion).
    const rows = selected.map((file) => {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = file.name;
        const pct = document.createElement("span");
        pct.className = "pct";
        pct.textContent = "0%";
        li.append(name, pct);
        list.append(li);
        return { file, li, pct, loaded: 0 };
    });
    const totalBytes = selected.reduce((sum, f) => sum + (f.size || 0), 0);
    wrap.hidden = false;
    fill.style.width = "0%";
    label.textContent = `Uploading ${selected.length} file(s)…`;

    function refreshAggregate() {
        const loaded = rows.reduce((sum, r) => sum + r.loaded, 0);
        const overall = totalBytes ? Math.round((loaded / totalBytes) * 100) : 0;
        fill.style.width = `${overall}%`;
        const remaining = rows.filter((r) => !r.li.classList.contains("done")
                                          && !r.li.classList.contains("failed")).length;
        const done = rows.length - remaining;
        label.textContent = remaining === 0
            ? "Done"
            : `Uploading ${done}/${rows.length} — ${overall}%`;
    }

    try {
        const { failures } = await api.uploadBatch(
            getSelectedFolderId(),
            selected,
            getSelectedRelDir(),
            {
                concurrency: 3,
                onFileProgress: (idx, _file, p) => {
                    rows[idx].loaded = p.loaded;
                    rows[idx].pct.textContent = `${Math.round(p.fraction * 100)}%`;
                    refreshAggregate();
                },
                onFileDone: (idx, file) => {
                    rows[idx].loaded = file.size || rows[idx].loaded;
                    rows[idx].li.classList.add("done");
                    rows[idx].pct.textContent = "✓";
                    refreshAggregate();
                },
                onFileError: (idx, _file, err) => {
                    rows[idx].li.classList.add("failed");
                    rows[idx].pct.textContent = "×";
                    rows[idx].li.title = err.message;
                    refreshAggregate();
                },
            },
        );
        if (failures.length) {
            label.textContent = `Done — ${failures.length} failed`;
        }
        // Leave the list visible long enough to read; clear once everything's
        // settled so the toolbar isn't permanently cluttered.
        setTimeout(() => {
            wrap.hidden = true;
            list.replaceChildren();
        }, failures.length ? 5000 : 1500);
    } catch (err) {
        wrap.hidden = true;
        list.replaceChildren();
        alert(err.message);
    } finally {
        e.target.value = "";
    }
});
