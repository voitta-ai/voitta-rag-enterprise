"""Static sanity checks for the SPA's ES modules.

The frontend has no test harness (plain ES modules, no bundler), so syntax
errors and module-wiring mistakes otherwise surface only in the browser at
runtime. This test runs ``node --check`` over every non-vendor JS file and
asserts the module graph's load-bearing wiring.

Skipped when node isn't installed (CI images without a JS toolchain).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_JS = Path(__file__).resolve().parents[2] / "static" / "js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _non_vendor_js() -> list[Path]:
    return sorted(
        p for p in STATIC_JS.rglob("*.js") if "vendor" not in p.parts
    )


def test_all_js_files_parse() -> None:
    files = _non_vendor_js()
    assert files, f"no JS files found under {STATIC_JS}"
    failures = []
    for f in files:
        proc = subprocess.run(
            ["node", "--check", str(f)], capture_output=True, text=True
        )
        if proc.returncode != 0:
            failures.append(f"{f.relative_to(STATIC_JS)}:\n{proc.stderr.strip()}")
    assert not failures, "JS syntax errors:\n" + "\n\n".join(failures)


def test_boot_imports_every_modal_and_preview_plugin() -> None:
    """Side-effect modules that nobody imports silently vanish from the app —
    their event listeners and registry entries never attach. Assert boot.js
    (the composition root) imports every modal module and preview plugin."""
    boot = (STATIC_JS / "boot.js").read_text()

    modals_dir = STATIC_JS / "modals"
    for modal in sorted(modals_dir.glob("*.js")):
        assert f"modals/{modal.name}" in boot, (
            f"boot.js does not import modals/{modal.name}"
        )
    # A future modals/<feature>/ package must be imported via its index.js.
    for pkg in sorted(p for p in modals_dir.iterdir() if p.is_dir()):
        assert f"modals/{pkg.name}/index.js" in boot, (
            f"boot.js does not import modals/{pkg.name}/index.js"
        )

    for plugin in sorted((STATIC_JS / "render" / "preview" / "plugins").glob("*.js")):
        assert f"plugins/{plugin.name}" in boot, (
            f"boot.js does not import preview plugin {plugin.name}"
        )
