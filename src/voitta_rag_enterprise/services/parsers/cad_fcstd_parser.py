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


def _clean_part_name(raw: str) -> str:
    return _FREECAD_INSTANCE.sub("", raw).rstrip(" _.") or raw.strip()


def _slugify(name: str, seen: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "component"
    slug = base
    n = 2
    while slug in seen:
        slug = f"{base}-{n}"
        n += 1
    seen.add(slug)
    return slug


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
    """One renderable bucket — either an App::Part (real container)
    or the synthetic "Unclassified" bucket for orphan Part::Features."""

    name: str
    parts: list[str] = field(default_factory=list)
    hardware: Counter = field(default_factory=Counter)
    # Zip-internal brp paths only. World transforms are *not* stored
    # here — they're composed at render time from a fresh Document.xml
    # parse. Stuffing them in this list bound transform-formula bugs
    # to the cached asset spec; recomputing keeps it a code-only fix.
    members: list[str] = field(default_factory=list)


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
) -> tuple[list[str], Counter, list[tuple[str, _Obj]]]:
    """Walk every descendant of ``root_name``, return (parts, hardware,
    feature_objs) for that subtree.

    ``feature_objs`` is the list of (zip_member_path, _Obj) pairs for
    every Part::Feature we touch — used downstream to compose the
    render-side member list. Traversal is depth-first; a cycle guard
    keeps us safe on pathological documents.
    """
    parts: list[str] = []
    hardware: Counter = Counter()
    feature_objs: list[tuple[str, _Obj]] = []
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
        # Recurse into children regardless of type — App::Part,
        # nested Mirror, etc. all have Group lists we should walk.
        stack.extend(obj.children)

    return parts, hardware, feature_objs


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


# ---------------------------------------------------------------------------
# Markdown emission
# ---------------------------------------------------------------------------


def _component_size(c: Component) -> int:
    return len(c.parts) + sum(c.hardware.values())


def build_markdown(
    file_stem: str,
    components: dict[str, Component],
) -> tuple[str, dict[str, tuple[str, list[str]]]]:
    """Return (markdown, slug_map) where slug_map is
    ``{slug → (component_name, [brp_path, ...])}``. Transforms are
    composed at render time, not stored here."""
    ordered = sorted(
        components.values(),
        key=lambda c: (-_component_size(c), c.name.lower()),
    )
    seen: set[str] = set()
    slug_map: dict[str, tuple[str, list[str]]] = {}
    for c in ordered:
        slug = _slugify(c.name, seen)
        slug_map[slug] = (c.name, list(c.members))

    lines: list[str] = [f"# {file_stem}", ""]
    lines.append(
        f"FreeCAD assembly · {len(ordered)} top-level components"
    )
    lines.append("")
    lines.append("## Components")
    lines.append("")
    lines.append(
        "Drill into any component below. Each is renderable with:"
    )
    lines.append(
        "  `request_asset(file_id=<N>, asset_type=\"cad_projection\", slug=...)`"
    )
    lines.append("")
    for slug, (comp_name, _) in slug_map.items():
        c = next(c for c in ordered if c.name == comp_name)
        lines.append(f"- `{slug}` — {c.name} ({_component_size(c)} parts)")
    lines.append("")
    for slug, (comp_name, _) in slug_map.items():
        c = next(c for c in ordered if c.name == comp_name)
        lines.append(f"## Component: {c.name}")
        lines.append("")
        lines.append(f"Slug: `{slug}`")
        lines.append(
            f"Renderable via: `request_asset(file_id=<N>, "
            f'asset_type="cad_projection", slug="{slug}")`'
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
    return "\n".join(lines), slug_map


def _emit_asset_specs(
    slug_map: dict[str, tuple[str, list[str]]],
) -> list[AssetSpec]:
    specs: list[AssetSpec] = []
    for slug, (comp_name, members) in slug_map.items():
        # Each member carries only the zip-internal brp path. The
        # render handler re-parses Document.xml on every request and
        # composes world transforms from the current parser logic.
        # Dict-of-one shape is kept (rather than a bare list of strings)
        # so future per-member metadata (color, material, hide flag)
        # can be added without invalidating cached specs.
        members_payload = [{"brp": brp_path} for brp_path in members]
        specs.append(
            AssetSpec(
                asset_type="cad_projection",
                label=f"Render projections of {comp_name}",
                description=(
                    "Returns four signed image URLs (front, top, side, iso) "
                    "for the named component. FCStd is re-parsed on every "
                    "request; component geometry composed from per-feature "
                    "BREP blobs in the archive."
                ),
                slug=slug,
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
                    # picks up this key and uses the
                    # FCStd code path. Coexists with STEP's
                    # ``x-entry-paths`` since both can't apply.
                    "x-fcstd-members": members_payload,
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

        components: dict[str, Component] = {}

        # Every App::Part as a potential renderable component. Each
        # rolls up its full subtree so the BOM matches what you see in
        # the FreeCAD Model panel for that part.
        assemblies = _user_visible_assemblies(by_name)
        single_root = _has_only_one_apppart_root(by_name)
        members_collected: set[str] = set()  # Part::Feature names we covered

        for root in assemblies:
            label = root.label or root.name
            parts, hardware, feats = _collect_members(by_name, root.name)
            members: list[str] = []
            for zip_path, fobj in feats:
                if zip_path not in brp_set:
                    continue  # silently skip features missing their brp
                members.append(zip_path)
                members_collected.add(fobj.name)
            comp = Component(
                name=label,
                parts=parts,
                hardware=hardware,
                members=members,
            )
            components[label] = comp

        # Orphan Part::Features (not under any App::Part) get an
        # Unclassified bucket. Rare in user-authored files but possible
        # for documents created by importing flat geometry.
        orphan_parts: list[str] = []
        orphan_hw: Counter = Counter()
        orphan_members: list[str] = []
        for o in by_name.values():
            if o.type != "Part::Feature":
                continue
            if o.name in members_collected:
                continue
            zip_path = f"{o.name}.Shape.brp"
            if zip_path not in brp_set:
                continue
            orphan_members.append(zip_path)
            label = o.label or o.name
            group, part = _route_label(label)
            if group is not None:
                orphan_hw[part] += 1
            elif part and part not in orphan_parts:
                orphan_parts.append(part)
        if orphan_members:
            components["Unclassified"] = Component(
                name="Unclassified",
                parts=orphan_parts,
                hardware=orphan_hw,
                members=orphan_members,
            )

        # Add a synthetic "Whole assembly" component ONLY when the
        # file doesn't already have a single named root that serves
        # the same purpose. If the user already has e.g. `4-Post Lift`
        # as the sole top-level App::Part, that IS the whole assembly
        # — adding a sibling slug for the same geometry just clutters
        # the menu. With multiple roots or none, the synthetic slug
        # is the only way to render everything in one shot.
        if single_root is None:
            whole_members: list[str] = []
            seen_brp: set[str] = set()
            whole_parts: list[str] = []
            whole_hw: Counter = Counter()
            for c in components.values():
                for brp in c.members:
                    if brp not in seen_brp:
                        seen_brp.add(brp)
                        whole_members.append(brp)
                for p in c.parts:
                    if p not in whole_parts:
                        whole_parts.append(p)
                for k, v in c.hardware.items():
                    whole_hw[k] += v
            if whole_members:
                components["Whole assembly"] = Component(
                    name="Whole assembly",
                    parts=whole_parts,
                    hardware=whole_hw,
                    members=whole_members,
                )

        if not components:
            return ParserResult(
                content=(
                    f"# {file_path.stem}\n\n"
                    f"FreeCAD assembly · 0 top-level components"
                )
            )

        content, slug_map = build_markdown(file_stem=file_path.stem, components=components)
        return ParserResult(
            content=content,
            on_demand_assets=_emit_asset_specs(slug_map),
        )


__all__ = ("CadFCStdParser",)
