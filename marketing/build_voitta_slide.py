#!/usr/bin/env python3
"""
Voitta marketing overview slide — McKinsey-inspired edition.
Run:    python build_voitta_slide.py
Output: voitta_overview.html
"""
import json, math

THEME = "dark"   # "dark" | "light" — controls shadows, dot grid, glows

# ── Palette: dark navy + single McKinsey teal accent ─────────────────────────
TEAL       = "#009DAE"   # McKinsey teal — the ONLY accent colour
BG         = "#070F1C"   # near-black canvas
NODE_FILL  = "#0C1B2A"   # source / output node fill
NODE_STROK = "#1A3350"   # source / output node border
VOITTA_BG  = "#030C17"   # Voitta interior (slightly darker)
TEXT_HI    = "#D8EAF6"   # primary text
TEXT_LO    = "#4A7090"   # secondary / subtitle text
EDGE_COL   = "#009DAE"   # all edges the same teal

# ── Node sizes ────────────────────────────────────────────────────────────────
WS, HS   = 268, 92       # source / output node width, height
GAP_Y    = 22            # vertical gap between sibling nodes
GAP_X    = 150           # horizontal column gap (edge travel space)
N_NODES  = 4

WV          = 300
CLUSTER_H   = N_NODES * HS + (N_NODES - 1) * GAP_Y   # 434
HV          = CLUSTER_H + 46                          # 480  (always taller)

PAD_X, PAD_Y = 72, 52
CANVAS_W  = PAD_X + WS + GAP_X + WV + GAP_X + WS + PAD_X
CANVAS_H  = max(CLUSTER_H, HV) + PAD_Y * 2

CY        = CANVAS_H / 2

SRC_X     = PAD_X
VOI_X     = SRC_X + WS + GAP_X
OUT_X     = VOI_X + WV + GAP_X
VOI_Y     = CY - HV / 2


def src_y(i):
    return CY - CLUSTER_H / 2 + i * (HS + GAP_Y)


# ── SVG helpers ───────────────────────────────────────────────────────────────

def side_node_svg(w, h, title, subtitle, is_output=False):
    rx = 8
    sh = round(h * 0.44)
    cx = w / 2
    shadow = f'<rect x="3" y="3" width="{w}" height="{h}" rx="{rx}" fill="rgba(0,0,0,0.4)"/>' if THEME == "dark" else ""
    sw = "1" if THEME == "dark" else "1.5"
    return (
        shadow +
        f'<rect width="{w}" height="{h}" rx="{rx}" fill="{NODE_FILL}" stroke="{NODE_STROK}" stroke-width="{sw}"/>'
        f'<line x1="18" y1="{sh}" x2="{w-18}" y2="{sh}" stroke="{NODE_STROK}" stroke-width="0.8"/>'
        f'<text x="{cx:.1f}" y="{sh*0.56:.1f}" text-anchor="middle"'
        f'  font-size="10.5" font-weight="700" fill="{TEXT_HI}" style="letter-spacing:1px">{title.upper()}</text>'
        f'<text x="{cx:.1f}" y="{sh+20:.1f}" text-anchor="middle"'
        f'  font-size="11.5" fill="{TEXT_HI}" opacity="0.55">{subtitle}</text>'
    )


def voitta_node_svg(w, h):
    cx = w / 2

    title_y  = h * 0.07
    sub_y    = title_y + 24
    div1_y   = sub_y + 18
    div2_y   = h - h * 0.10
    icon_cy  = (div1_y + div2_y) / 2
    icon_h   = div2_y - div1_y

    perim = round(2 * (w + h) - (8 - 2 * math.pi) * 4 * 4)
    snake = round(perim * 0.72)
    gap   = perim - snake

    glow = "".join(
        f'<rect x="{-x}" y="{-x}" width="{w+x*2}" height="{h+x*2}" rx="{18+x}"'
        f'  fill="none" stroke="{TEAL}" stroke-width="1" opacity="{op}"/>'
        for x, op in [(18, "0.03"), (9, "0.07"), (3, "0.16"), (1, "0.28")]
    )

    out = []

    # Shadow + chassis
    if THEME == "dark":
        out.append(f'<rect x="4" y="4" width="{w}" height="{h}" rx="18" fill="rgba(0,0,0,0.55)"/>')
    out.append(f'<rect width="{w}" height="{h}" rx="18" fill="{VOITTA_BG}" stroke="{TEAL}" stroke-width="1.5"/>')
    if THEME == "dark":
        out.append(glow)
    out.append(
        f'<rect width="{w}" height="{h}" rx="18" fill="none"'
        f'  stroke="{TEAL}" stroke-width="2" stroke-linecap="round"'
        f'  stroke-dasharray="{snake} {gap}"'
        f'  style="animation:voitta-border 3.2s linear infinite"/>'
    )

    # Dividers
    out.append(f'<line x1="22" y1="{div1_y:.1f}" x2="{w-22}" y2="{div1_y:.1f}" stroke="{TEAL}" stroke-width="0.6" opacity="0.4"/>')
    out.append(f'<line x1="22" y1="{div2_y:.1f}" x2="{w-22}" y2="{div2_y:.1f}" stroke="{TEAL}" stroke-width="0.6" opacity="0.4"/>')

    # Title
    # only apply glow on the coloured (teal) dark variant, not B&W dark
    title_filter = 'filter:url(#glow-text);' if (THEME == "dark" and TEAL != "#ffffff") else ''
    out.append(
        f'<text x="{cx:.1f}" y="{title_y+16:.1f}" text-anchor="middle"'
        f'  font-size="18" font-weight="800" fill="{TEAL}"'
        f'  style="letter-spacing:7px;{title_filter}">VOITTA</text>'
    )
    out.append(
        f'<text x="{cx:.1f}" y="{sub_y+13:.1f}" text-anchor="middle"'
        f'  font-size="11" fill="{TEXT_HI}" style="letter-spacing:4px;opacity:0.75">AI</text>'
    )

    # ── Abstract geometry — minimal, suggests complexity ──────────────────────
    # Two large partial arcs (open, not full circles)
    r1 = icon_h * 0.38
    r2 = icon_h * 0.24
    r3 = icon_h * 0.13

    m = 1.0 if THEME == "dark" else 3.5   # opacity multiplier for light theme

    # Outer partial arc
    out.append(
        f'<circle cx="{cx:.1f}" cy="{icon_cy:.1f}" r="{r1:.1f}"'
        f'  fill="none" stroke="{TEAL}" stroke-width="0.8" opacity="{min(0.2*m,1):.2f}"'
        f'  stroke-dasharray="{r1*4.7:.1f} {r1*1.5:.1f}"/>'
    )
    # Mid arc
    out.append(
        f'<circle cx="{cx:.1f}" cy="{icon_cy:.1f}" r="{r2:.1f}"'
        f'  fill="none" stroke="{TEAL}" stroke-width="1" opacity="{min(0.35*m,1):.2f}"'
        f'  stroke-dasharray="{r2*3.8:.1f} {r2*2.5:.1f}"'
        f'  style="transform-box:fill-box;transform-origin:center;transform:rotate(110deg)"/>'
    )
    # Inner ring
    out.append(f'<circle cx="{cx:.1f}" cy="{icon_cy:.1f}" r="{r3:.1f}" fill="none" stroke="{TEAL}" stroke-width="1.2" opacity="{min(0.55*m,1):.2f}"/>')

    # Core dot
    core_filter = 'filter:url(#glow-text);' if (THEME == "dark" and TEAL != "#ffffff") else ''
    out.append(f'<circle cx="{cx:.1f}" cy="{icon_cy:.1f}" r="4" fill="{TEAL}" opacity="0.85" style="{core_filter}"/>')

    # Radiating spokes
    for a_deg, length_frac in [(15, 0.85), (72, 0.6), (135, 0.9), (190, 0.7), (248, 0.55), (310, 0.8)]:
        a  = math.radians(a_deg)
        x0 = cx + math.cos(a) * (r3 + 4)
        y0 = icon_cy + math.sin(a) * (r3 + 4)
        x1 = cx + math.cos(a) * r1 * length_frac
        y1 = icon_cy + math.sin(a) * r1 * length_frac
        out.append(
            f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}"'
            f'  stroke="{TEAL}" stroke-width="0.8" opacity="{min(0.3*m,1):.2f}"/>'
        )
        out.append(f'<circle cx="{x1:.1f}" cy="{y1:.1f}" r="2" fill="{TEAL}" opacity="{min(0.5*m,1):.2f}"/>')

    # Off-axis network nodes
    node_pts = [
        (cx - r1*0.55, icon_cy - r1*0.30),
        (cx + r1*0.48, icon_cy - r1*0.20),
        (cx + r1*0.10, icon_cy + r1*0.52),
    ]
    for i, (nx, ny) in enumerate(node_pts):
        out.append(f'<line x1="{cx:.1f}" y1="{icon_cy:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{TEAL}" stroke-width="0.6" opacity="{min(0.18*m,1):.2f}"/>')
        nx2, ny2 = node_pts[(i+1) % 3]
        out.append(f'<line x1="{nx:.1f}" y1="{ny:.1f}" x2="{nx2:.1f}" y2="{ny2:.1f}" stroke="{TEAL}" stroke-width="0.5" opacity="{min(0.12*m,1):.2f}"/>')
        out.append(f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="3" fill="none" stroke="{TEAL}" stroke-width="1" opacity="{min(0.45*m,1):.2f}"/>')
        out.append(f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="1.2" fill="{TEAL}" opacity="{min(0.6*m,1):.2f}"/>')

    # Bottom label
    out.append(
        f'<text x="{cx:.1f}" y="{div2_y+18:.1f}" text-anchor="middle"'
        f'  font-size="9" fill="{TEXT_HI}" style="letter-spacing:2px;opacity:0.6">'
        f'WORKS ACROSS ALL CONNECTED SYSTEMS</text>'
    )

    return "".join(out)


# ── Node definitions ──────────────────────────────────────────────────────────
SOURCE_DEFS = [
    ("Documents & Files",     "NFS · Shared Storage · ERP"),
    ("Project & Work Tracking","Project Management · Change Control"),
    ("Source & Version Control","Code · Config · Release History"),
    ("Engineering & Design",   "PLM · CAD · BOMs · specifications"),
]

OUTPUT_DEFS = [
    ("Reports & Presentations", "Built from live data"),
    ("Answers & Summaries",     "Conversational access to all your data"),
    ("Data Exports",            "Ready for analytics & machine learning"),
    ("Automated Workflows",     "Agentic, repeatable pipelines"),
]


def build():
    # ── Compute node positions ────────────────────────────────────────────────
    nodes = {}  # id → {x, y, w, h, svg}

    for i, (title, sub) in enumerate(SOURCE_DEFS):
        nid = f"src_{i}"
        nodes[nid] = {
            "x": SRC_X, "y": src_y(i), "w": WS, "h": HS,
            "svg": side_node_svg(WS, HS, title, sub),
        }

    nodes["voitta"] = {
        "x": VOI_X, "y": VOI_Y, "w": WV, "h": HV,
        "svg": voitta_node_svg(WV, HV),
    }

    for i, (title, sub) in enumerate(OUTPUT_DEFS):
        nid = f"out_{i}"
        nodes[nid] = {
            "x": OUT_X, "y": src_y(i), "w": WS, "h": HS,
            "svg": side_node_svg(WS, HS, title, sub, is_output=True),
        }

    # ── Compute edge paths ────────────────────────────────────────────────────
    vn   = nodes["voitta"]
    vcx  = vn["x"] + vn["w"] / 2
    vcy  = vn["y"] + vn["h"] / 2

    # Spread port Y positions across 60% of Voitta height
    port_spread = vn["h"] * 0.60
    port_ys = [vn["y"] + vn["h"] * 0.20 + i * port_spread / (N_NODES - 1)
               for i in range(N_NODES)]

    # Stepped (H→V→H) connectors — elbow at midpoint of each gap
    mid_left  = (vn["x"] + SRC_X + WS) / 2   # midpoint of left gap
    mid_right = (vn["x"] + WV + OUT_X) / 2   # midpoint of right gap

    edge_paths = []
    for i in range(N_NODES):
        sn   = nodes[f"src_{i}"]
        sy   = sn["y"] + sn["h"] / 2          # source centre Y
        py   = port_ys[i]                      # port Y on Voitta left side
        p0x  = sn["x"] + sn["w"]
        p3x  = vn["x"]
        d    = f"M {p0x} {sy:.1f} H {mid_left:.1f} V {py:.1f} H {p3x}"
        edge_paths.append((d, sy, py, "left"))

    for i in range(N_NODES):
        on   = nodes[f"out_{i}"]
        oy   = on["y"] + on["h"] / 2          # output centre Y
        py   = port_ys[i]                      # port Y on Voitta right side
        p0x  = vn["x"] + vn["w"]
        p3x  = on["x"]
        d    = f"M {p0x} {py:.1f} H {mid_right:.1f} V {oy:.1f} H {p3x}"
        edge_paths.append((d, oy, py, "right"))

    # ── Render ────────────────────────────────────────────────────────────────
    node_elems = []
    for nid, n in nodes.items():
        node_elems.append(
            f'<g transform="translate({n["x"]:.1f},{n["y"]:.1f})">'
            + n["svg"] + "</g>"
        )

    edge_elems = []
    arrow_elems = []
    SIZES = [1.50, 1.35, 1.60, 1.25]
    for idx, (d, src_cy, port_y, side) in enumerate(edge_paths):
        dur = f"{SIZES[idx % 4]:.2f}s"
        if THEME == "dark":
            edge_elems.append(
                f'<path d="{d}" fill="none" stroke="{EDGE_COL}"'
                f'  stroke-width="5" opacity="0.07"/>'
            )
        edge_elems.append(
            f'<path d="{d}" fill="none" stroke="{EDGE_COL}"'
            f'  stroke-width="1.8" stroke-dasharray="8 4"'
            f'  stroke-linecap="square"'
            f'  style="animation:dash-march {dur} linear infinite"/>'
        )
    # Arrowheads on output edges (tip at output node left edge)
    SZ = 10
    for i in range(N_NODES):
        on   = nodes[f"out_{i}"]
        tx   = on["x"]
        ty   = on["y"] + on["h"] / 2
        arrow_elems.append(
            f'<polygon points="{tx},{ty:.1f} {tx-SZ},{ty-SZ*0.44:.1f} {tx-SZ},{ty+SZ*0.44:.1f}"'
            f'  fill="{EDGE_COL}"/>'
        )

    # Dotgrid background — suppressed on light theme
    if THEME == "dark":
        dot_circles = []
        for gx in range(0, int(CANVAS_W) + 30, 28):
            for gy in range(0, int(CANVAS_H) + 28, 28):
                dot_circles.append(
                    f'<circle cx="{gx}" cy="{gy}" r="0.9" fill="#152535" opacity="0.7"/>'
                )
        dotgrid = "\n".join(dot_circles)
    else:
        dotgrid = ""

    # Ambient glow — dark only
    ambient = (
        f'<ellipse cx="{VOI_X + WV/2:.1f}" cy="{CY:.1f}"'
        f'  rx="{WV*0.85:.1f}" ry="{HV*0.6:.1f}"'
        f'  fill="{TEAL}" opacity="0.04"'
        f'  filter="url(#voitta-ambient)"/>'
    ) if THEME == "dark" else ""

    label_src = (
        f'<text x="{SRC_X + WS/2:.1f}" y="{CY - CLUSTER_H/2 - 18:.1f}"'
        f'  text-anchor="middle" font-size="12" font-weight="600" fill="{TEXT_HI}" opacity="0.75"'
        f'  style="letter-spacing:3px">DATA SOURCES</text>'
        f'<line x1="{SRC_X + WS*0.1:.1f}" y1="{CY - CLUSTER_H/2 - 8:.1f}"'
        f'  x2="{SRC_X + WS*0.9:.1f}" y2="{CY - CLUSTER_H/2 - 8:.1f}"'
        f'  stroke="{TEXT_HI}" stroke-width="0.7" opacity="0.2"/>'
    )
    label_out = (
        f'<text x="{OUT_X + WS/2:.1f}" y="{CY - CLUSTER_H/2 - 18:.1f}"'
        f'  text-anchor="middle" font-size="12" font-weight="600" fill="{TEXT_HI}" opacity="0.75"'
        f'  style="letter-spacing:3px">KNOWLEDGE</text>'
        f'<line x1="{OUT_X + WS*0.1:.1f}" y1="{CY - CLUSTER_H/2 - 8:.1f}"'
        f'  x2="{OUT_X + WS*0.9:.1f}" y2="{CY - CLUSTER_H/2 - 8:.1f}"'
        f'  stroke="{TEXT_HI}" stroke-width="0.7" opacity="0.2"/>'
    )

    vb_pad = 30
    vbx = -vb_pad
    vby = -vb_pad
    vbw = CANVAS_W + vb_pad * 2
    vbh = CANVAS_H + vb_pad * 2
    border_perim = round(2 * (WV + HV) - (8 - 2 * math.pi) * 4 * 4)
    border_snake = round(border_perim * 0.72)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voitta — Unified Semantic Layer</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    width: 100vw; height: 100vh; overflow: hidden;
    background: {BG};
    font-family: -apple-system, "SF Pro Display", "Helvetica Neue", "Segoe UI", sans-serif;
  }}
  #page {{
    display: flex; flex-direction: column; width: 100vw; height: 100vh;
  }}
  #diagram-wrap {{
    flex: 1; min-height: 0;
    display: flex; align-items: center; justify-content: center;
    padding: 10px 20px 14px;
  }}
  #diagram {{ display: block; }}

  @keyframes dash-march    {{ to {{ stroke-dashoffset: -24; }} }}
  @keyframes voitta-border {{ to {{ stroke-dashoffset: -{border_snake}; }} }}
</style>
</head>
<body>
<div id="page">
  <div id="diagram-wrap">
    <svg id="diagram" xmlns="http://www.w3.org/2000/svg"
         viewBox="{vbx} {vby} {vbw:.0f} {vbh:.0f}"
         preserveAspectRatio="xMidYMid meet">
      <defs>
        <filter id="glow-text" x="-30%" y="-60%" width="160%" height="220%">
          <feGaussianBlur in="SourceGraphic" stdDeviation="{2 if TEAL == '#ffffff' else 5}" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="voitta-ambient" x="-80%" y="-60%" width="260%" height="220%">
          <feGaussianBlur in="SourceGraphic" stdDeviation="48" result="b"/>
          <feColorMatrix in="b" type="matrix"
            values="0 0 0 0 0   0 0 0 0 0.62   0 0 0 0 0.68   0 0 0 0.5 0"/>
        </filter>
      </defs>

      <!-- dotgrid -->
      {dotgrid}

      <!-- ambient glow -->
      {ambient}

      <!-- edges (behind nodes) -->
      {''.join(edge_elems)}
      {''.join(arrow_elems)}

      <!-- column labels -->
      {label_src}
      {label_out}

      <!-- nodes -->
      {''.join(node_elems)}
    </svg>
  </div>
</div>
<script>
(function() {{
  const wrap = document.getElementById("diagram-wrap");
  const svg  = document.getElementById("diagram");
  const vbW  = {vbw:.0f};
  const vbH  = {vbh:.0f};
  function resize() {{
    const availW = wrap.clientWidth  - 24;
    const availH = wrap.clientHeight - 16;
    const scale  = Math.min(availW / vbW, availH / vbH);
    svg.setAttribute("width",  Math.round(vbW * scale));
    svg.setAttribute("height", Math.round(vbH * scale));
  }}
  resize();
  window.addEventListener("resize", resize);
}})();
</script>
</body>
</html>"""


def _with_palette(palette, filename, theme="dark"):
    global TEAL, BG, NODE_FILL, NODE_STROK, VOITTA_BG, TEXT_HI, TEXT_LO, EDGE_COL, THEME
    _orig = (TEAL, BG, NODE_FILL, NODE_STROK, VOITTA_BG, TEXT_HI, TEXT_LO, EDGE_COL, THEME)
    (TEAL, BG, NODE_FILL, NODE_STROK, VOITTA_BG, TEXT_HI, TEXT_LO, EDGE_COL) = palette
    THEME = theme
    html = build()
    (TEAL, BG, NODE_FILL, NODE_STROK, VOITTA_BG, TEXT_HI, TEXT_LO, EDGE_COL, THEME) = _orig
    with open(filename, "w") as f:
        f.write(html)
    print(f"Written → {filename}")


if __name__ == "__main__":
    # 1 — original (dark navy + teal)
    with open("voitta_overview.html", "w") as f:
        f.write(build())
    print("Written → voitta_overview.html")

    # 2 — B&W dark
    _with_palette((
        "#ffffff",   # TEAL  → white accent
        "#0a0a0a",   # BG    → near-black
        "#141414",   # NODE_FILL
        "#2e2e2e",   # NODE_STROK
        "#080808",   # VOITTA_BG
        "#f0f0f0",   # TEXT_HI
        "#888888",   # TEXT_LO
        "#ffffff",   # EDGE_COL
    ), "voitta_overview_dark_bw.html")

    # 3 — B&W light
    _with_palette((
        "#111111",   # TEAL  → near-black accent
        "#ffffff",   # BG    → white
        "#ffffff",   # NODE_FILL → pure white
        "#aaaaaa",   # NODE_STROK → mid-grey border
        "#f9f9f9",   # VOITTA_BG → very pale
        "#111111",   # TEXT_HI → near-black
        "#555555",   # TEXT_LO → mid-grey
        "#333333",   # EDGE_COL → dark grey
    ), "voitta_overview_light_bw.html", theme="light")
