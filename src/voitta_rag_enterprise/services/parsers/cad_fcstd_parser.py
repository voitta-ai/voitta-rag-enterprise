"""FreeCAD ``.FCStd`` parser.

FCStd is a zip archive containing:

* ``Document.xml`` — the FreeCAD document tree (objects, types,
  labels, parent-child links via ``App::Part.Group``).
* ``*.brp`` — per-feature B-rep blobs (OpenCASCADE's native format,
  same kernel that reads STEP).

Why we support this format alongside STEP

FreeCAD's STEP exporter is lossy: it flattens LinkGroups, drops
parametric history, and (the user's own bug report) can fail
round-tripping its own export. The native FCStd preserves the *real*
assembly hierarchy via App::Part containers — no `::` / `[group]`
name-convention regex needed for the top-level grouping. We still
apply those conventions to label *parts* inside a component because
the user's CAD pipeline uses them as part-naming discipline.

Addressing granularity
----------------------
Every ``Part::Feature`` is its own addressable component, identified
by a **label path** (e.g. ``4-post-lift/base-frame/longitudinal-rail-l``).
Each ``App::Part`` container is also addressable (renders the
subtree). Both forms accept the same MCP calls — ``cad_projection``
for PNG views, ``cad_mesh`` for a three.js-ready GLB. The path
scheme is filesystem-shaped on purpose: agents that walk
``list_assets`` can navigate by prefix, and the LLM can ask for
``4-post-lift/base-frame`` to get the sub-assembly or drill down to
a single rail.

Slug uniqueness
---------------
Within a given parent, sibling slugs are made unique by appending
``-2``/``-3``/... when two siblings slugify to the same string.
Across the whole document, full paths inherit uniqueness from this
per-scope disambiguation.

Internal name path
------------------
We also record the chain of FreeCAD internal names
(``Part001 / Part005 / Body017``) on the AssetSpec's ``params_schema``
as ``x-fcstd-internal-path``. The label path is the user-facing slug;
the internal path is the stable identifier that round-trips across
renames in the FreeCAD UI. Renderers don't need the internal path
(they use ``x-fcstd-members``); it's there for diagnostics and so
future tools can pivot on the FreeCAD identity rather than the
human label.

Geometry handling

Each ``Part::Feature`` corresponds to ``<object_name>.Shape.brp``
inside the archive. To get a renderable shape we extract the brp on
demand and feed it to ``BRepTools.Read``. We never need the full
parametric history (sketches / features / expressions) — those
exist for the human author, not the LLM.

Placement composition

A part's world transform is the matrix product of every
``App::Part``'s ``Placement`` along the path from root to its
*immediate parent App::Part*. The leaf Part::Feature's own
``Placement`` is **not** included — FreeCAD bakes a feature's
Placement into the saved shape's OCC ``Location`` (see
``Part::Feature::onChanged`` in upstream FreeCAD), so reading the
``.brp`` back already gives us geometry positioned in the parent
App::Part's coordinate frame. Adding the leaf placement on top
would double-apply it.

Composition runs at **render time**, not parse time: the asset spec
written to CAS stores only the brp paths grouped by slug. Document.xml
is re-parsed on every render request (~50 ms on a 3 MB file with 670
features — negligible vs the ~1.5 s tessellate+render cost). This
keeps the transform formula a code-only concern; fixing a placement
bug no longer requires re-indexing every FCStd ever uploaded.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ..asset_handlers import AssetSpec
from .base import BaseParser, ParserResult

logger = logging.getLogger(__name__)


# Same conventions as the STEP parser — applied to *labels* of leaf
# parts to disambiguate fabricated vs hardware listings in the BOM.
# Top-level grouping doesn't use these; it uses the App::Part tree.
_DOUBLE_COLON = re.compile(r"^(?P<group>.+?)\s+::\s+(?P<part>.+)$")
_BRACKET_GROUP = re.compile(r"^(?P<part>.+?)\s*\[(?P<group>[^\]]+)\]\s*$")
_FREECAD_INSTANCE = re.compile(r"(?<=[^\s\d])\d{3,}$")

# Property names that look like free-form engineering annotations.
# FreeCAD's docs recommend ``Description`` but authors aren't strict
# about spelling — accept the common variants. Lowercase compare.
_NOTE_PROP_NAMES = frozenset({
    "description", "descriptions",
    "note", "notes",
    "comment", "comments",
    "remark", "remarks",
    "engineeringnote", "engineering_note", "engineeringnotes",
})


def _clean_part_name(raw: str) -> str:
    return _FREECAD_INSTANCE.sub("", raw).rstrip(" _.") or raw.strip()


def _slugify_segment(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "component"


def _slugify(name: str, seen: set[str]) -> str:
    """Legacy single-segment slugifier kept for backward-compat callers
    (tests, the synthetic ``Whole assembly`` slug). New code uses
    ``_slugify_path`` to produce hierarchical slugs."""
    base = _slugify_segment(name)
    slug = base
    n = 2
    while slug in seen:
        slug = f"{base}-{n}"
        n += 1
    seen.add(slug)
    return slug


def _slugify_path(
    segments: tuple[str, ...],
    parent_seen: dict[tuple[str, ...], set[str]],
) -> str:
    """Slugify the leaf segment with per-parent disambiguation.

    ``parent_seen[parent_segments]`` tracks the slugified leaf segments
    already used by siblings under the same parent path. The full slug
    is ``parent-slug/leaf-slug`` joined by ``/`` — uniqueness across
    the whole document follows from per-scope disambiguation.
    """
    parent = segments[:-1]
    leaf = segments[-1]
    seen = parent_seen.setdefault(parent, set())
    base = _slugify_segment(leaf)
    slug_leaf = base
    n = 2
    while slug_leaf in seen:
        slug_leaf = f"{base}-{n}"
        n += 1
    seen.add(slug_leaf)
    # Parent path slug: each segment slugified independently, joined
    # by '/'. Sibling uniqueness at every level was already enforced
    # when the parent was registered, so a plain slugify is fine here.
    parent_slug_segments = [_slugify_segment(s) for s in parent]
    if parent_slug_segments:
        return "/".join(parent_slug_segments + [slug_leaf])
    return slug_leaf


# ---------------------------------------------------------------------------
# Document.xml parsing
# ---------------------------------------------------------------------------


@dataclass
class _Obj:
    name: str                       # FreeCAD internal name (e.g. ``Part__Feature``)
    type: str                       # FreeCAD class ('App::Part', 'Part::Feature', ...)
    label: str                      # human label ('Floor pulley RR-a')
    parent: str | None = None       # parent's internal name; None for roots
    children: list[str] = field(default_factory=list)   # internal names
    placement: tuple[float, ...] = (0, 0, 0, 0, 0, 0, 1)  # px, py, pz, q0..q3
    # Free-form text annotations. ``description`` is the convention
    # FreeCAD authors use for engineering notes attached to a part:
    # ``obj.addProperty("App::PropertyString", "Description", "Notes", ...)``
    # We collect *any* string property whose name suggests an annotation
    # (Description, Note, Notes, Comment, Remark) so authors aren't
    # locked to one exact spelling.
    notes: dict[str, str] = field(default_factory=dict)
    # Spreadsheet cells when ``type == "Spreadsheet::Sheet"`` — a
    # mapping of cell address (``A1``) to its content string. The
    # content is what the user typed; cells holding formulas keep
    # the formula source (``=1+1`` not ``2``). Empty for non-sheets.
    cells: dict[str, str] = field(default_factory=dict)


def _parse_document_xml(raw: bytes) -> dict[str, _Obj]:
    """Parse Document.xml into ``{name → _Obj}`` with parent links resolved.

    We make two passes: one to collect every object's type + name from
    ``<Objects>``, then we read properties from ``<ObjectData>``
    (Label, Placement, Group). Group lists give us child→parent
    links; we invert them so each object knows its parent.

    Returns an empty dict if the file isn't parseable as FreeCAD XML
    (in which case the caller surfaces it as a parse failure).
    """
    root = ET.fromstring(raw)
    objs_el = root.find("Objects")
    data_el = root.find("ObjectData")
    if objs_el is None or data_el is None:
        return {}

    by_name: dict[str, _Obj] = {}
    for o in objs_el.findall("Object"):
        name = o.get("name") or ""
        if not name:
            continue
        by_name[name] = _Obj(name=name, type=o.get("type", "?"), label="")

    for d in data_el.findall("Object"):
        name = d.get("name") or ""
        obj = by_name.get(name)
        if obj is None:
            continue
        for p in d.findall("Properties/Property"):
            pname = p.get("name", "")
            ptype = p.get("type", "")
            if pname == "Label":
                s = p.find("String")
                if s is not None:
                    obj.label = s.get("value") or ""
            elif pname == "Placement" and ptype == "App::PropertyPlacement":
                place = p.find("PropertyPlacement")
                if place is not None:
                    obj.placement = (
                        float(place.get("Px", 0)),
                        float(place.get("Py", 0)),
                        float(place.get("Pz", 0)),
                        float(place.get("Q0", 0)),
                        float(place.get("Q1", 0)),
                        float(place.get("Q2", 0)),
                        float(place.get("Q3", 1)),
                    )
            elif pname == "Group" and ptype.endswith("LinkList"):
                for link in p.findall("LinkList/Link"):
                    child = link.get("value")
                    if child and child in by_name:
                        obj.children.append(child)
                        by_name[child].parent = name
            elif ptype == "App::PropertyString" and pname.lower() in _NOTE_PROP_NAMES:
                # Engineering note attached to the part. The String
                # element carries the actual text; treat empty/missing
                # as "not really annotated" and skip.
                s = p.find("String")
                if s is not None:
                    val = (s.get("value") or "").strip()
                    if val:
                        obj.notes[pname] = val
            elif pname == "cells" and obj.type == "Spreadsheet::Sheet":
                # ``Spreadsheet::Sheet`` stores its data as a property
                # named ``cells`` (lowercase, per Spreadsheet/App/Sheet.cpp).
                # The inner element is ``<Cells Count="N" xlink="1">``
                # holding one ``<Cell address="A1" content="..." />``
                # per used cell. Formulas keep their source string;
                # we surface that as-is so the indexer can search for
                # both literal text and formula expressions.
                cells_el = p.find("Cells")
                if cells_el is not None:
                    for cell in cells_el.findall("Cell"):
                        addr = cell.get("address") or ""
                        content = cell.get("content") or ""
                        if addr and content:
                            obj.cells[addr] = content

    return by_name


# ---------------------------------------------------------------------------
# Placement composition
# ---------------------------------------------------------------------------


def _placement_to_matrix(placement: tuple[float, ...]) -> tuple[float, ...]:
    """Convert (px, py, pz, q0, q1, q2, q3) → flat 16-element 4x4 matrix
    in column-major order (matches OCC + VTK convention)."""
    px, py, pz, q0, q1, q2, q3 = placement
    # Normalize quaternion (FreeCAD usually does, but be defensive)
    n = math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3) or 1.0
    q0, q1, q2, q3 = q0 / n, q1 / n, q2 / n, q3 / n
    # Standard quaternion → 3x3 rotation
    r00 = 1 - 2 * (q1 * q1 + q2 * q2)
    r01 = 2 * (q0 * q1 - q2 * q3)
    r02 = 2 * (q0 * q2 + q1 * q3)
    r10 = 2 * (q0 * q1 + q2 * q3)
    r11 = 1 - 2 * (q0 * q0 + q2 * q2)
    r12 = 2 * (q1 * q2 - q0 * q3)
    r20 = 2 * (q0 * q2 - q1 * q3)
    r21 = 2 * (q1 * q2 + q0 * q3)
    r22 = 1 - 2 * (q0 * q0 + q1 * q1)
    # Return row-major 4x4 with translation in last column
    # (caller decides whether to transpose for column-major frameworks).
    return (
        r00, r01, r02, px,
        r10, r11, r12, py,
        r20, r21, r22, pz,
        0.0, 0.0, 0.0, 1.0,
    )


def _matmul4(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """4x4 matrix multiply, row-major. Hand-rolled to avoid pulling
    numpy into the parser's import path."""
    return tuple(
        a[r * 4 + 0] * b[0 + c]
        + a[r * 4 + 1] * b[4 + c]
        + a[r * 4 + 2] * b[8 + c]
        + a[r * 4 + 3] * b[12 + c]
        for r in range(4)
        for c in range(4)
    )


def _world_transform(by_name: dict[str, _Obj], leaf_name: str) -> tuple[float, ...]:
    """Walk parent → ... → root, accumulating App::Part placements.

    The leaf Part::Feature's own ``Placement`` is intentionally
    excluded: FreeCAD bakes a feature's Placement into the underlying
    TopoDS_Shape's OCC ``Location`` (see ``Part::Feature::onChanged`` →
    ``shape.setTransform(Placement.toMatrix())``), and that location
    is preserved in the saved ``.brp``. Including the leaf placement
    here would double-apply it when the render path composes our
    transform with the shape's existing location.
    """
    chain: list[tuple[float, ...]] = []
    leaf = by_name.get(leaf_name)
    if leaf is None:
        return (1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0)
    cursor = by_name.get(leaf.parent) if leaf.parent else None
    while cursor is not None:
        chain.append(_placement_to_matrix(cursor.placement))
        cursor = by_name.get(cursor.parent) if cursor.parent else None
    if not chain:
        return (1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0)
    # chain = [parent, grandparent, ..., root]
    # world = root * ... * grandparent * parent
    out = chain[-1]
    for m in reversed(chain[:-1]):
        out = _matmul4(out, m)
    return out


# ---------------------------------------------------------------------------
# Component aggregation
# ---------------------------------------------------------------------------


@dataclass
class Component:
    """An addressable bucket in the FCStd asset menu.

    Four flavours, distinguished by ``kind``:

    * ``"container"`` — an ``App::Part``. ``members`` are every brp
      under its subtree. Rendering gives the whole sub-assembly.
    * ``"feature"`` — a single ``Part::Feature``. ``members`` is one
      brp. Rendering gives just that part.
    * ``"orphans"`` — synthetic catch-all for ``Part::Feature`` nodes
      not under any ``App::Part``.
    * ``"whole"`` — synthetic "render everything" when the file lacks
      a single top-level App::Part to play that role.

    ``path_segments`` is the ancestor-label chain ending in this
    component's own label; ``internal_path_segments`` is the same
    chain in FreeCAD internal names. ``slug`` is the user-facing
    path-style identifier (e.g. ``base-frame/longitudinal-rail-l``).
    """

    name: str
    kind: str = "container"
    path_segments: tuple[str, ...] = ()
    internal_path_segments: tuple[str, ...] = ()
    slug: str = ""
    parts: list[str] = field(default_factory=list)
    hardware: Counter = field(default_factory=Counter)
    # Zip-internal brp paths only. World transforms are *not* stored
    # here — they're composed at render time from a fresh Document.xml
    # parse. Stuffing them in this list bound transform-formula bugs
    # to the cached asset spec; recomputing keeps it a code-only fix.
    members: list[str] = field(default_factory=list)
    # Per-part engineering annotations collected from the descendant
    # tree. Each entry is ``(label_or_name, prop_name, value)`` —
    # exposing the prop_name lets the indexer chunk by property type
    # (every Description in the model, every Note, etc.) without
    # losing the per-part context. Order is depth-first traversal.
    notes: list[tuple[str, str, str]] = field(default_factory=list)


def _route_label(label: str) -> tuple[str | None, str | None]:
    """Apply the FreeCAD label conventions to extract (group, part).

    Top-level grouping comes from the App::Part tree, not this — this
    is only used to discriminate **hardware** from **fabricated parts**
    inside an already-routed component. Returns ``(group, part)``
    where ``group`` is the bracket-suffix when present (signals
    hardware), or ``None`` for fabricated parts."""
    if not label or label.startswith("Origin"):
        return None, None
    m = _DOUBLE_COLON.match(label)
    if m:
        return None, m.group("part").strip()
    m = _BRACKET_GROUP.match(label)
    if m:
        return m.group("group").strip(), _clean_part_name(m.group("part"))
    return None, label


def _collect_members(
    by_name: dict[str, _Obj], root_name: str
) -> tuple[list[str], Counter, list[tuple[str, _Obj]], list[tuple[str, str, str]]]:
    """Walk every descendant of ``root_name``, return ``(parts,
    hardware, feature_objs, notes)`` for that subtree.

    ``feature_objs`` is the list of (zip_member_path, _Obj) pairs for
    every Part::Feature we touch — used downstream to compose the
    render-side member list. ``notes`` collects every engineering
    annotation found on any descendant (not just Part::Features —
    an App::Part container can carry a Description too) as
    ``(displayed_name, prop_name, value)`` tuples. Traversal is
    depth-first; a cycle guard keeps us safe on pathological documents.
    """
    parts: list[str] = []
    hardware: Counter = Counter()
    feature_objs: list[tuple[str, _Obj]] = []
    notes: list[tuple[str, str, str]] = []
    visited: set[str] = set()
    stack: list[str] = [root_name]

    while stack:
        name = stack.pop()
        if name in visited:
            continue
        visited.add(name)
        obj = by_name.get(name)
        if obj is None:
            continue
        if obj.type == "Part::Feature":
            zip_path = f"{obj.name}.Shape.brp"
            feature_objs.append((zip_path, obj))
            label = obj.label or obj.name
            group, part = _route_label(label)
            if group is not None:
                hardware[part] += 1
            elif part:
                if part not in parts:
                    parts.append(part)
        # Engineering notes can live on Part::Features or on the
        # App::Part containers themselves. Surface them either way
        # so a per-assembly Description doesn't get lost just
        # because the container has no geometry of its own.
        if obj.notes:
            display = obj.label or obj.name
            for prop, value in obj.notes.items():
                notes.append((display, prop, value))
        # Recurse into children regardless of type — App::Part,
        # nested Mirror, etc. all have Group lists we should walk.
        stack.extend(obj.children)

    return parts, hardware, feature_objs, notes


def _user_visible_assemblies(by_name: dict[str, _Obj]) -> list[_Obj]:
    """Return every App::Part that's worth showing as a component.

    A FreeCAD assembly often has a hierarchy like
    ``[4-Post Lift] → [Runway A, Runway B, Floor pulley RR-a, ...]``
    where the outermost App::Part is a wrapper for everything. We
    want the user to be able to render BOTH the wrapper (whole
    assembly) AND each named sub-assembly independently — so we
    expose every App::Part regardless of depth.

    Excluded: App::Parts named ``Origin*`` (these are FreeCAD's
    per-body coordinate-system containers; geometry-less).
    """
    return [
        o for o in by_name.values()
        if o.type == "App::Part" and not (o.label or o.name).startswith("Origin")
    ]


def _has_only_one_apppart_root(by_name: dict[str, _Obj]) -> _Obj | None:
    """If there's a single root App::Part (no App::Part ancestors) and
    its child tree contains every other App::Part, return it.

    Used by the parser to decide whether to skip adding a synthetic
    ``Whole assembly`` slug — if the single root IS the whole
    assembly, naming it redundantly would clutter the menu.
    """
    roots: list[_Obj] = []
    for o in by_name.values():
        if o.type != "App::Part" or (o.label or o.name).startswith("Origin"):
            continue
        cursor = by_name.get(o.parent) if o.parent else None
        has_ancestor = False
        while cursor is not None:
            if cursor.type == "App::Part":
                has_ancestor = True
                break
            cursor = by_name.get(cursor.parent) if cursor.parent else None
        if not has_ancestor:
            roots.append(o)
    return roots[0] if len(roots) == 1 else None


def _container_ancestor_chain(
    by_name: dict[str, _Obj], obj: _Obj
) -> list[_Obj]:
    """Return the chain of App::Part ancestors from root → immediate parent.

    The leaf object itself is NOT included. Origin containers are
    skipped (they're FreeCAD's coordinate-system markers, not part of
    the user-facing hierarchy)."""
    chain: list[_Obj] = []
    cursor = by_name.get(obj.parent) if obj.parent else None
    while cursor is not None:
        if cursor.type == "App::Part" and not (cursor.label or cursor.name).startswith("Origin"):
            chain.append(cursor)
        cursor = by_name.get(cursor.parent) if cursor.parent else None
    chain.reverse()
    return chain


# ---------------------------------------------------------------------------
# Markdown emission
# ---------------------------------------------------------------------------


def _component_size(c: Component) -> int:
    return len(c.parts) + sum(c.hardware.values())


def _component_sort_key(c: Component) -> tuple[int, str]:
    """Sort containers before features, then by name."""
    kind_order = {"whole": 0, "container": 1, "orphans": 2, "feature": 3}
    return (kind_order.get(c.kind, 4), c.name.lower())


def build_markdown(
    file_stem: str,
    components: list[Component],
    spreadsheets: list[_Obj] | None = None,
) -> str:
    """Emit Markdown with one ``## Component:`` section per addressable
    component. The companion ``CadComponentStrategy`` chunker splits on
    these headings so each component lands in its own chunk — that's
    what makes search return per-component hits with the slug visible
    in the chunk text.

    Path layout: the index lists every slug as ``- `<slug>` — Label``;
    feature-level entries are indented under their container so the
    LLM can read the tree without re-fetching Document.xml.
    """
    feature_count = sum(1 for c in components if c.kind == "feature")
    container_count = sum(1 for c in components if c.kind == "container")

    lines: list[str] = [f"# {file_stem}", ""]
    lines.append(
        f"FreeCAD assembly · {container_count} containers, "
        f"{feature_count} features, {len(components)} addressable components"
    )
    lines.append("")
    lines.append("## Components")
    lines.append("")
    lines.append(
        "Every entry below is addressable as ``slug=<path>`` against "
        "``cad_projection`` (PNG views) and ``cad_mesh`` (glTF binary "
        "for three.js). Paths are filesystem-shaped: an App::Part "
        "renders its subtree; a Part::Feature renders just that part."
    )
    lines.append("")
    indexed = sorted(components, key=lambda c: c.slug)
    for c in indexed:
        depth = max(0, c.slug.count("/"))
        indent = "  " * depth
        kind_tag = {
            "container": " (sub-assembly)",
            "feature": "",
            "orphans": " (orphans)",
            "whole": " (whole)",
        }.get(c.kind, "")
        lines.append(f"{indent}- `{c.slug}` — {c.name}{kind_tag}")
    lines.append("")

    ordered = sorted(components, key=_component_sort_key)
    for c in ordered:
        lines.append(f"## Component: {c.name}")
        lines.append("")
        lines.append(f"Slug: `{c.slug}`")
        if c.internal_path_segments:
            lines.append(
                f"Internal path: `{' / '.join(c.internal_path_segments)}`"
            )
        kind_human = {
            "container": "App::Part container (renders subtree)",
            "feature": "Part::Feature (single addressable part)",
            "orphans": "Orphans (Part::Features outside any App::Part)",
            "whole": "Synthetic whole-assembly aggregate",
        }.get(c.kind, c.kind)
        lines.append(f"Kind: {kind_human}")
        lines.append("")
        lines.append("Renderable via:")
        lines.append(
            f'- `request_asset(file_id=<N>, asset_type="cad_projection", '
            f'slug="{c.slug}")`'
        )
        lines.append(
            f'- `request_asset(file_id=<N>, asset_type="cad_mesh", '
            f'slug="{c.slug}")`'
        )
        lines.append("")
        if c.parts:
            lines.append(f"### Fabricated parts ({len(c.parts)})")
            for pn in c.parts:
                lines.append(f"- {pn}")
            lines.append("")
        if c.hardware:
            n = sum(c.hardware.values())
            lines.append(f"### Standard hardware ({n})")
            for pn, qty in c.hardware.most_common():
                lines.append(f"- {qty}× {pn}")
            lines.append("")
        if c.notes:
            lines.append(f"### Engineering notes ({len(c.notes)})")
            for display, prop, value in c.notes:
                lines.append(f"- **{display}** — _{prop}_: {value}")
            lines.append("")
    if spreadsheets:
        # Sheets with no used cells are dropped at the parse step,
        # so reaching here means there's at least one cell to print.
        lines.append("## Spreadsheets")
        lines.append("")
        lines.append(
            "Embedded Spreadsheet workbench tabs. Each row below "
            "shows ``<address>: <content>``; formulas keep their "
            "source expression rather than the evaluated value."
        )
        lines.append("")
        for sheet in spreadsheets:
            sheet_label = sheet.label or sheet.name
            lines.append(f"### Sheet: {sheet_label}")
            lines.append("")
            for addr in _sorted_cell_addresses(sheet.cells):
                content = sheet.cells[addr]
                lines.append(f"- `{addr}`: {content}")
            lines.append("")
    return "\n".join(lines)


def _sorted_cell_addresses(cells: dict[str, str]) -> list[str]:
    """Sort cell addresses in spreadsheet-natural order: row first,
    then column. ``A1, B1, A2`` becomes ``A1, B1, A2`` rather than
    the lexicographic ``A1, A2, B1``."""
    def key(addr: str) -> tuple[int, int]:
        col_letters = "".join(ch for ch in addr if ch.isalpha())
        row_digits = "".join(ch for ch in addr if ch.isdigit())
        col = 0
        for ch in col_letters.upper():
            col = col * 26 + (ord(ch) - ord("A") + 1)
        row = int(row_digits) if row_digits else 0
        return (row, col)
    return sorted(cells, key=key)


def _emit_asset_specs(
    components: list[Component],
) -> list[AssetSpec]:
    """One AssetSpec per addressable component. ``cad_mesh`` reuses
    these via the same slug at render time (no separate spec needed —
    ``cad_mesh.spec_for`` only emits the whole-file menu entry; the
    per-slug specs come from here)."""
    specs: list[AssetSpec] = []
    for c in components:
        members_payload = [{"brp": brp_path} for brp_path in c.members]
        path_label = " / ".join(c.path_segments) if c.path_segments else c.name
        specs.append(
            AssetSpec(
                asset_type="cad_projection",
                label=f"Render projections of {path_label}",
                description=(
                    "Returns four signed image URLs (front, top, side, iso) "
                    "for the named component. The same slug works against "
                    "``cad_mesh`` for a glTF binary suitable for three.js. "
                    "FCStd is re-parsed on every request; geometry composed "
                    "from per-feature BREP blobs in the archive."
                ),
                slug=c.slug,
                params_schema={
                    "type": "object",
                    "properties": {
                        "size": {
                            "type": "integer",
                            "minimum": 64,
                            "maximum": 2048,
                            "default": 320,
                        }
                    },
                    "additionalProperties": False,
                    # FCStd-specific render hint; the render handler
                    # picks up this key and uses the FCStd code path.
                    # Coexists with STEP's ``x-entry-paths`` since both
                    # can't apply.
                    "x-fcstd-members": members_payload,
                    # Stable FreeCAD identity for diagnostics + future
                    # tools that need to round-trip back to the source
                    # document. Not consumed by the renderer.
                    "x-fcstd-internal-path": list(c.internal_path_segments),
                    # User-facing path segments — same info the slug
                    # encodes, but unslugified so consumers don't have
                    # to reverse-engineer.
                    "x-fcstd-path": list(c.path_segments),
                    # ``"container"``/``"feature"``/``"orphans"``/``"whole"``
                    # so the LLM can prefer feature-level addressing when
                    # it knows what it wants and fall back to containers
                    # for sub-assemblies.
                    "x-fcstd-kind": c.kind,
                },
                examples=(
                    {"size": 320},
                    {"size": 1024},
                ),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class CadFCStdParser(BaseParser):
    """Parse a FreeCAD .FCStd file into an LLM-searchable component
    tree. Same on-demand render contract as the STEP parser; the
    handler reads the file's path from the File row and the
    per-component member list from the on_demand_assets menu."""

    extensions: ClassVar[list[str]] = [".fcstd"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            with zipfile.ZipFile(file_path) as z:
                if "Document.xml" not in z.namelist():
                    return ParserResult.failure(
                        "FCStd missing Document.xml — not a FreeCAD file?"
                    )
                xml_raw = z.read("Document.xml")
                brp_set = {n for n in z.namelist() if n.endswith(".brp")}
        except zipfile.BadZipFile as e:
            return ParserResult.failure(f"FCStd is not a valid zip: {e}")
        except OSError as e:
            return ParserResult.failure(f"FCStd read failed: {e}")

        try:
            by_name = _parse_document_xml(xml_raw)
        except ET.ParseError as e:
            return ParserResult.failure(f"FCStd Document.xml malformed: {e}")

        if not by_name:
            return ParserResult.failure("FCStd has no Objects in Document.xml")

        components: list[Component] = []
        parent_seen: dict[tuple[str, ...], set[str]] = {}
        # Slug → Component, used to find the right Component to append
        # to when a feature is re-encountered (shouldn't happen, but
        # defensive against weird FreeCAD docs).
        by_slug: dict[str, Component] = {}

        # Pass 1: containers. Visit in BFS root-first order so parent
        # containers are registered (and slug-scoped) before children.
        assemblies = _user_visible_assemblies(by_name)
        single_root = _has_only_one_apppart_root(by_name)
        members_collected: set[str] = set()  # Part::Feature names we covered

        # Sort containers so roots come first — important for the
        # per-parent slug-disambiguation scoping.
        def _container_depth(o: _Obj) -> int:
            return len(_container_ancestor_chain(by_name, o))

        for root in sorted(assemblies, key=_container_depth):
            chain = _container_ancestor_chain(by_name, root)
            path_segments = tuple(
                (a.label or a.name) for a in chain
            ) + ((root.label or root.name),)
            internal_segments = tuple(a.name for a in chain) + (root.name,)
            slug = _slugify_path(path_segments, parent_seen)

            parts, hardware, feats, notes = _collect_members(by_name, root.name)
            members: list[str] = []
            for zip_path, fobj in feats:
                if zip_path not in brp_set:
                    continue
                members.append(zip_path)
                members_collected.add(fobj.name)
            comp = Component(
                name=root.label or root.name,
                kind="container",
                path_segments=path_segments,
                internal_path_segments=internal_segments,
                slug=slug,
                parts=parts,
                hardware=hardware,
                members=members,
                notes=notes,
            )
            components.append(comp)
            by_slug[slug] = comp

        # Pass 2: every Part::Feature gets its own slug, regardless of
        # whether it lives under an App::Part. Orphans (no container)
        # are collected separately for the synthetic "orphans" bucket.
        orphan_features: list[_Obj] = []
        for o in by_name.values():
            if o.type != "Part::Feature":
                continue
            zip_path = f"{o.name}.Shape.brp"
            if zip_path not in brp_set:
                continue

            chain = _container_ancestor_chain(by_name, o)
            if not chain:
                orphan_features.append(o)
                continue

            path_segments = tuple(
                (a.label or a.name) for a in chain
            ) + ((o.label or o.name),)
            internal_segments = tuple(a.name for a in chain) + (o.name,)
            slug = _slugify_path(path_segments, parent_seen)

            label = o.label or o.name
            group, part = _route_label(label)
            parts: list[str] = []
            hardware = Counter()
            if group is not None:
                hardware[part] += 1
            elif part:
                parts.append(part)

            notes: list[tuple[str, str, str]] = []
            if o.notes:
                display = o.label or o.name
                for prop, value in o.notes.items():
                    notes.append((display, prop, value))

            comp = Component(
                name=label,
                kind="feature",
                path_segments=path_segments,
                internal_path_segments=internal_segments,
                slug=slug,
                parts=parts,
                hardware=hardware,
                members=[zip_path],
                notes=notes,
            )
            components.append(comp)
            by_slug[slug] = comp
            members_collected.add(o.name)

        # Orphan Part::Features get a single "Unclassified" container
        # AND each gets its own feature-level slug under that container.
        if orphan_features:
            orphan_root_segments = ("Unclassified",)
            orphan_root_slug = _slugify_path(orphan_root_segments, parent_seen)
            orphan_parts: list[str] = []
            orphan_hw: Counter = Counter()
            orphan_members: list[str] = []
            orphan_notes: list[tuple[str, str, str]] = []
            for o in orphan_features:
                zip_path = f"{o.name}.Shape.brp"
                orphan_members.append(zip_path)
                label = o.label or o.name
                group, part = _route_label(label)
                if group is not None:
                    orphan_hw[part] += 1
                elif part and part not in orphan_parts:
                    orphan_parts.append(part)
                if o.notes:
                    display = o.label or o.name
                    for prop, value in o.notes.items():
                        orphan_notes.append((display, prop, value))

                # Per-feature slug under the synthetic orphan container.
                f_path = orphan_root_segments + (label,)
                f_internal = ("",) + (o.name,)
                f_slug = _slugify_path(f_path, parent_seen)
                f_notes: list[tuple[str, str, str]] = []
                if o.notes:
                    display = o.label or o.name
                    for prop, value in o.notes.items():
                        f_notes.append((display, prop, value))
                comp = Component(
                    name=label,
                    kind="feature",
                    path_segments=f_path,
                    internal_path_segments=f_internal,
                    slug=f_slug,
                    parts=[part] if part and group is None else [],
                    hardware=(Counter([part]) if part and group is not None else Counter()),
                    members=[zip_path],
                    notes=f_notes,
                )
                components.append(comp)
                by_slug[f_slug] = comp

            orphan_comp = Component(
                name="Unclassified",
                kind="orphans",
                path_segments=orphan_root_segments,
                internal_path_segments=(),
                slug=orphan_root_slug,
                parts=orphan_parts,
                hardware=orphan_hw,
                members=orphan_members,
                notes=orphan_notes,
            )
            components.append(orphan_comp)
            by_slug[orphan_root_slug] = orphan_comp

        # Synthetic "whole assembly" slug only when the file doesn't
        # already have a single root App::Part to fill that role. With
        # a single root, that root IS the whole assembly; adding a
        # sibling slug for the same geometry just clutters the menu.
        if single_root is None:
            whole_members: list[str] = []
            seen_brp: set[str] = set()
            whole_parts: list[str] = []
            whole_hw: Counter = Counter()
            whole_notes: list[tuple[str, str, str]] = []
            seen_notes: set[tuple[str, str, str]] = set()
            for c in components:
                # Only roll up container-kind components into "whole";
                # feature-level components are already covered by their
                # containers (and rolling them up here would double-
                # count parts in the BOM).
                if c.kind != "container":
                    continue
                for brp in c.members:
                    if brp not in seen_brp:
                        seen_brp.add(brp)
                        whole_members.append(brp)
                for p in c.parts:
                    if p not in whole_parts:
                        whole_parts.append(p)
                for k, v in c.hardware.items():
                    whole_hw[k] += v
                for note in c.notes:
                    if note not in seen_notes:
                        seen_notes.add(note)
                        whole_notes.append(note)
            if whole_members:
                whole_segments = ("Whole assembly",)
                whole_slug = _slugify_path(whole_segments, parent_seen)
                components.append(Component(
                    name="Whole assembly",
                    kind="whole",
                    path_segments=whole_segments,
                    internal_path_segments=(),
                    slug=whole_slug,
                    parts=whole_parts,
                    hardware=whole_hw,
                    members=whole_members,
                    notes=whole_notes,
                ))

        # Collect every Spreadsheet::Sheet with at least one used cell.
        spreadsheets = [
            o for o in by_name.values()
            if o.type == "Spreadsheet::Sheet" and o.cells
        ]
        spreadsheets.sort(key=lambda o: (o.label or o.name).lower())

        if not components and not spreadsheets:
            return ParserResult(
                content=(
                    f"# {file_path.stem}\n\n"
                    f"FreeCAD assembly · 0 top-level components"
                )
            )

        content = build_markdown(
            file_stem=file_path.stem,
            components=components,
            spreadsheets=spreadsheets,
        )
        return ParserResult(
            content=content,
            on_demand_assets=_emit_asset_specs(components),
        )


__all__ = ("CadFCStdParser",)
