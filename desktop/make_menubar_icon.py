#!/usr/bin/env python3
"""Generate the menu-bar status icon for the Voitta RAG desktop app.

A flat monochrome "V RAG" wordmark on a transparent background — a macOS
*template* image. Template images are drawn with black + alpha; the system
tints them to match the menu bar (black on light, white on dark), so the item
looks native next to the other monochrome status icons rather than a clashing
colored pill. The shell loads it with template=True and no title (rumps' legacy
NSStatusItem API won't show a title and image together). Supersampled 4× then
downscaled for crisp antialiasing.

    ./desktop/make_menubar_icon.py
    → src/voitta_rag_desktop/resources/voitta-menubar.png  (height 36 = retina 2×)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLACK = (0, 0, 0, 255)  # template images are black + alpha; macOS tints them
TEXT = "RAG"
H = 36          # final height in px (menu bar glyph ≈ 18pt → 2× retina)
SS = 4          # supersample factor
FONT = "/System/Library/Fonts/SFNS.ttf"  # San Francisco — matches the system
FONT_FRAC = 0.74  # cap height as a fraction of image height

OUT = (
    Path(__file__).resolve().parent.parent
    / "src/voitta_rag_desktop/resources/voitta-menubar.png"
)


def main() -> None:
    h = H * SS
    font = ImageFont.truetype(FONT, int(h * FONT_FRAC))

    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    l, t, r, b = probe.textbbox((0, 0), TEXT, font=font)
    tw, th = r - l, b - t
    pad_x = int(h * 0.10)
    w = tw + pad_x * 2

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text(((w - tw) / 2 - l, (h - th) / 2 - t), TEXT, font=font, fill=BLACK)

    img = img.resize((round(w / SS), H), Image.LANCZOS)
    img.save(OUT)
    print(f"wrote {OUT}  ({img.size[0]}×{img.size[1]}, template/monochrome)")


if __name__ == "__main__":
    main()
