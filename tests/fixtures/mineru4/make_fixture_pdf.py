"""Build the synthetic PDFs the MinerU 4 fixtures were recorded from.

Run once (needs ``reportlab``, ``pypdf`` and Pillow, all pulled in by
``mineru``), then record the parser output with ``record_fixture.py``:

    python tests/fixtures/mineru4/make_fixture_pdf.py tests/fixtures/mineru4

Writes:

* ``sample.pdf`` — 3 pages: headings + body text and a bar chart (p1), a
  table with a caption (p2), a two-column page with a list (p3).
* ``encrypted.pdf`` — ``sample.pdf`` behind a user password.

The content is synthetic so the fixtures can live in the repo.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as RLImage,
)

BODY = (
    "Regional revenue grew in every quarter of the fiscal year, led by the "
    "northern territories where the new distribution centre opened in March. "
    "Operating margin improved by two points as freight costs normalised and "
    "the product mix shifted towards higher-value subscriptions."
)


def _chart_png() -> bytes:
    img = Image.new("RGB", (900, 540), "white")
    d = ImageDraw.Draw(img)
    d.line([(80, 470), (860, 470)], fill="black", width=3)
    d.line([(80, 470), (80, 40)], fill="black", width=3)
    for i, h in enumerate([120, 210, 290, 380]):
        x0 = 140 + i * 180
        d.rectangle([x0, 470 - h, x0 + 110, 470], fill=(40, 90, 170))
        d.text((x0 + 35, 485), f"Q{i + 1}", fill="black")
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_sample(path: Path) -> None:
    styles = getSampleStyleSheet()
    w, h = letter
    margin = 0.9 * inch
    full = Frame(margin, margin, w - 2 * margin, h - 2 * margin, id="full")
    col_w = (w - 2 * margin - 0.4 * inch) / 2
    left = Frame(margin, margin, col_w, h - 2 * margin, id="left")
    right = Frame(margin + col_w + 0.4 * inch, margin, col_w, h - 2 * margin, id="right")
    doc = BaseDocTemplate(str(path), pagesize=letter, title="Fixture annual review")
    doc.addPageTemplates(
        [
            PageTemplate(id="one", frames=[full]),
            PageTemplate(id="two", frames=[left, right]),
        ]
    )

    story = [
        Paragraph("Annual Operating Review", styles["Title"]),
        Paragraph("1. Revenue by quarter", styles["Heading1"]),
        Paragraph(BODY, styles["BodyText"]),
        Spacer(1, 12),
        RLImage(io.BytesIO(_chart_png()), width=5 * inch, height=3 * inch),
        Paragraph("Figure 1. Revenue by quarter, in millions of dollars.", styles["Italic"]),
        Spacer(1, 12),
        Paragraph(BODY.replace("Regional", "Group"), styles["BodyText"]),
        PageBreak(),
        Paragraph("2. Segment results", styles["Heading1"]),
        Paragraph("Table 1. Segment revenue and margin.", styles["Italic"]),
    ]
    rows = [["Segment", "Revenue ($m)", "Margin (%)"]] + [
        [name, f"{rev:,.1f}", f"{m:.1f}"]
        for name, rev, m in [
            ("Hardware", 412.5, 18.2),
            ("Software", 288.0, 41.7),
            ("Services", 196.3, 22.9),
            ("Licensing", 74.8, 63.1),
        ]
    ]
    table = Table(rows, colWidths=[2.2 * inch, 1.6 * inch, 1.4 * inch])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ]
        )
    )
    story += [
        table,
        Spacer(1, 12),
        Paragraph(BODY.replace("Regional revenue", "Segment profit"), styles["BodyText"]),
        NextPageTemplate("two"),
        PageBreak(),
        Paragraph("3. Outlook", styles["Heading1"]),
    ]
    for i in range(4):
        story.append(Paragraph(f"Paragraph {i + 1} of the outlook. " + BODY, styles["BodyText"]))
    story.append(
        ListFlowable(
            [
                ListItem(Paragraph(t, styles["BodyText"]))
                for t in (
                    "Open two further distribution centres.",
                    "Retire the legacy billing platform.",
                    "Grow subscription revenue to half of the total.",
                )
            ],
            bulletType="bullet",
        )
    )
    story.append(FrameBreak())
    for i in range(4):
        story.append(
            Paragraph(f"Right column paragraph {i + 1}. " + BODY, styles["BodyText"])
        )
    doc.build(story)


def build_encrypted(src: Path, path: Path) -> None:
    writer = PdfWriter()
    for page in PdfReader(str(src)).pages:
        writer.add_page(page)
    writer.encrypt(user_password="fixture-secret", owner_password="fixture-owner")
    with path.open("wb") as fh:
        writer.write(fh)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    build_sample(out / "sample.pdf")
    build_encrypted(out / "sample.pdf", out / "encrypted.pdf")
    print(out / "sample.pdf", out / "encrypted.pdf")


if __name__ == "__main__":
    main()
