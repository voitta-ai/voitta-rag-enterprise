#!/usr/bin/env python3
"""Generate the macOS app icon (.icns) for the Voitta RAG desktop app.

A white "RAG" wordmark on the brand-navy tile — same tile as the old "V" icon,
matching the "RAG" menu-bar glyph (see make_menubar_icon.py). Renders a 1024×
master PNG, downscales it into a full .iconset, and runs ``iconutil`` to emit
voitta.icns (the name desktop/pyproject.toml's ``icon = …/voitta`` references).

    ./desktop/make_app_icon.py
    → src/voitta_rag_desktop/resources/voitta.icns
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

NAVY = (15, 23, 42, 255)  # sampled from the existing icon
WHITE = (255, 255, 255, 255)
TEXT = "RAG"
MASTER = 1024
SS = 2  # supersample the master before the final 1024 downscale
FONT = "/System/Library/Fonts/SFNS.ttf"  # San Francisco — matches the menu glyph
FONT_FRAC = 0.40  # cap height as a fraction of the tile

RES = Path(__file__).resolve().parent.parent / "src/voitta_rag_desktop/resources"
OUT = RES / "voitta.icns"

# (iconset filename, pixel size) — the set macOS expects.
ICONSET = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def _render_master() -> Image.Image:
    s = MASTER * SS
    img = Image.new("RGBA", (s, s), NAVY)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, int(s * FONT_FRAC))
    l, t, r, b = d.textbbox((0, 0), TEXT, font=font)
    tw, th = r - l, b - t
    d.text(((s - tw) / 2 - l, (s - th) / 2 - t), TEXT, font=font, fill=WHITE)
    return img.resize((MASTER, MASTER), Image.LANCZOS)


def main() -> None:
    master = _render_master()
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "voitta.iconset"
        iconset.mkdir()
        for name, size in ICONSET:
            master.resize((size, size), Image.LANCZOS).save(iconset / name)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)],
            check=True,
        )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
