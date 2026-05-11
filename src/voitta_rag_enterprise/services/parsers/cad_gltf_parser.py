"""glTF / glb parser — CAD assemblies, tree-as-RAG.

We treat a glTF file as a directory of named components and produce a
markdown table-of-contents the embedder can index. **No images at
extract time**: every render is on-demand via the ``cad_projection``
asset handler.

The design rests on two name conventions FreeCAD's glTF exporter
emits — see ``_route_name`` for the regex pass.

Why trimesh? Pure-pip, parses both .glb and .gltf, exposes the scene
graph as Python dicts, lets us pull subgraphs out cheaply at render
time. We never re-link the kernel — no C++ surprises.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ..asset_handlers import AssetSpec
from .base import BaseParser, ParserResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Name routing — :: and [group] conventions
# ---------------------------------------------------------------------------


# ``Group :: part``
_DOUBLE_COLON = re.compile(r"^(?P<group>.+?)\s+::\s+(?P<part>.+)$")
# ``Hardware-name [Group]``
_BRACKET_GROUP = re.compile(r"^(?P<part>.+?)\s*\[(?P<group>[^\]]+)\]\s*$")
# trimesh expands repeated nodes into unique graph names by appending
# ``_<6+ hex digits>`` to the source glTF name. Then if a node appears
# *inside* multiple parents, it can be re-suffixed (``_3``, ``_4``).
# We strip both classes greedily from the right.
_TRIMESH_HEX_SUFFIX = re.compile(r"(?:_[0-9a-f]{6,})+$")
_TRIMESH_SMALL_NUM_SUFFIX = re.compile(r"(?:_\d{1,3})+$")
# FreeCAD instance suffix on hardware: 3+ digits glued to the end of
# the part name with no separator (``Bearing 60206 (GOST 7242)003``,
# ``Hex nut M8005``). Real part numbers always sit on the right side
# of a space or letter; the FreeCAD counter sits directly after a
# closing paren or a letter with no whitespace. We only strip when
# preceded by a non-space non-digit — that distinguishes
# ``Bearing 60206`` (real, 6-digit part number after a space) from
# ``Bearing 60206003`` (real + 3-digit instance counter, contiguous).
_FREECAD_INSTANCE = re.compile(r"(?<=[^\s\d])\d{3,}$")


def _strip_trimesh_suffix(name: str) -> str:
    """Drop trimesh's per-instance suffixes (hex hash + small int)."""
    out = name
    # Apply both kinds, possibly repeatedly — order matters: the hex
    # block usually appears outermost.
    for _ in range(3):
        before = out
        out = _TRIMESH_HEX_SUFFIX.sub("", out)
        out = _TRIMESH_SMALL_NUM_SUFFIX.sub("", out)
        out = out.rstrip("_")
        if out == before:
            break
    return out


def _clean_part_name(raw: str) -> str:
    """Strip both trimesh + FreeCAD instance suffixes so hardware aggregates."""
    name = _strip_trimesh_suffix(raw)
    name = _FREECAD_INSTANCE.sub("", name).rstrip(" _.")
    return name or raw.strip()


@dataclass
class Component:
    """A routed top-level component.

    ``parts`` accumulates the de-duplicated fabricated parts (from the
    ``::`` convention). ``hardware`` is a count-per-name dict (from the
    ``[group]`` convention) so the markdown can show "8× Bearing
    60206" without listing each instance.
    """

    name: str
    parts: list[str] = field(default_factory=list)
    hardware: Counter = field(default_factory=Counter)
    # All trimesh-graph node names belonging to this component — the
    # render handler uses these to isolate the subgraph at render time.
    node_names: list[str] = field(default_factory=list)


def _route_name(raw_name: str) -> tuple[str | None, str | None]:
    """Return ``(group, part)`` for a glTF node name.

    ``group`` is None for unroutable names; the caller buckets those
    under an "Unclassified" group instead of failing.
    """
    name = _strip_trimesh_suffix(raw_name)
    if not name or name.startswith("Origin"):
        return None, None
    m = _DOUBLE_COLON.match(name)
    if m:
        return m.group("group").strip(), m.group("part").strip()
    m = _BRACKET_GROUP.match(name)
    if m:
        return m.group("group").strip(), _clean_part_name(m.group("part"))
    # No convention matched. Fall back to "Unclassified" so the node
    # still appears in the BOM, but we don't fake a group from a name
    # that doesn't tell us one.
    return None, name


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
# Tree extraction
# ---------------------------------------------------------------------------


def extract_components(scene_graph_nodes: list[str]) -> dict[str, Component]:
    """Route every named node into its parent component.

    Pure function — takes the list of node names, returns the routed
    dict. Used by both the parser (at extract time, to build markdown)
    and the render handler (at request time, to look up which nodes
    belong to the requested slug). Re-running over the same names
    produces identical output, so the slug map is stable as long as
    the underlying file bytes are.

    Routing source decides hardware-vs-fabricated:

    * Names matching ``::`` are *fabricated* parts (placement inside a
      LinkGroup). The part name may itself contain ``[xyz]`` — that's
      an inventory tag (e.g. ``sheave [101]``), not a group annotation.

    * Names matching ``[group]`` only (no ``::``) are hardware: the
      bracketed token IS the group, and the same physical part appears
      N times across instances with a FreeCAD ``003`` counter suffix.
      Aggregate by name with a Counter.
    """
    components: dict[str, Component] = {}
    for raw in scene_graph_nodes:
        stripped = _strip_trimesh_suffix(raw)
        if not stripped or stripped.startswith("Origin"):
            continue

        m_dc = _DOUBLE_COLON.match(stripped)
        if m_dc:
            group = m_dc.group("group").strip()
            part = m_dc.group("part").strip()
            comp = components.setdefault(group, Component(name=group))
            comp.node_names.append(raw)
            if part and part not in comp.parts:
                comp.parts.append(part)
            continue

        m_br = _BRACKET_GROUP.match(stripped)
        if m_br:
            group = m_br.group("group").strip()
            part = _clean_part_name(m_br.group("part"))
            comp = components.setdefault(group, Component(name=group))
            comp.node_names.append(raw)
            if part:
                comp.hardware[part] += 1
            continue

        # Unrouted name — bucket under "Unclassified" so it's still
        # searchable. We use the trimesh-stripped name verbatim (not
        # the part-cleaner): unrouted nodes are typically structured
        # part numbers like ``Assembly 01-153.02.402`` where the
        # trailing digits ARE the identifier, not an instance counter.
        comp = components.setdefault("Unclassified", Component(name="Unclassified"))
        comp.node_names.append(raw)
        if stripped not in comp.parts:
            comp.parts.append(stripped)
    return components


# ---------------------------------------------------------------------------
# Markdown emission
# ---------------------------------------------------------------------------


def _component_summary(c: Component) -> int:
    return len(c.parts) + sum(c.hardware.values())


def build_markdown(
    file_stem: str,
    components: dict[str, Component],
    triangle_count: int,
    generator: str | None,
) -> tuple[str, dict[str, str]]:
    """Build the file's markdown content + the canonical slug map.

    Returns ``(content, slug_by_component)``. ``slug_by_component`` is
    a deterministic dict keyed by component name; the parser hands it
    to ``_emit_asset_specs`` so :func:`request_asset` can later look
    up component → slug and the render handler can do the inverse.
    """
    # Deterministic order: descending by part count, then by name.
    # Stable across re-extracts of identical bytes.
    ordered = sorted(
        components.values(),
        key=lambda c: (-_component_summary(c), c.name.lower()),
    )
    seen_slugs: set[str] = set()
    slugs = {c.name: _slugify(c.name, seen_slugs) for c in ordered}

    lines: list[str] = []
    lines.append(f"# {file_stem}")
    lines.append("")
    summary_bits = [
        f"CAD assembly · {len(ordered)} top-level components",
        f"{triangle_count:,} triangles",
    ]
    if generator:
        summary_bits.append(f"Generator: {generator}")
    lines.append(" · ".join(summary_bits))
    lines.append("")

    # Root navigation chunk — the LLM lands here first when searching
    # the assembly by name; drills into a component by reading its slug.
    lines.append("## Components")
    lines.append("")
    lines.append("Drill into any component below. Each is renderable with:")
    lines.append("  `request_asset(file_id=<N>, asset_type=\"cad_projection\", slug=...)`")
    lines.append("")
    for c in ordered:
        slug = slugs[c.name]
        size = _component_summary(c)
        # Each row stays short — the LLM should be able to scan 30+
        # components in a single chunk without choking.
        lines.append(f"- `{slug}` — {c.name} ({size} parts)")
    lines.append("")

    # Per-component chunks. Heading-driven chunking puts each under
    # its own ## block, which makes them retrievable independently
    # via search.
    for c in ordered:
        slug = slugs[c.name]
        lines.append(f"## Component: {c.name}")
        lines.append("")
        lines.append(f"Slug: `{slug}`")
        lines.append(f"Renderable via: `request_asset(file_id=<N>, "
                     f'asset_type="cad_projection", slug="{slug}")`')
        lines.append("")
        if c.parts:
            lines.append(f"### Fabricated parts ({len(c.parts)})")
            for p in c.parts:
                lines.append(f"- {p}")
            lines.append("")
        if c.hardware:
            n = sum(c.hardware.values())
            lines.append(f"### Standard hardware ({n})")
            # Most-common first; same name disambiguation as parts.
            for part, qty in c.hardware.most_common():
                lines.append(f"- {qty}× {part}")
            lines.append("")
    return "\n".join(lines), slugs


def _emit_asset_specs(slugs: dict[str, str]) -> list[AssetSpec]:
    """One AssetSpec per component → drives ``list_assets`` output."""
    specs: list[AssetSpec] = []
    for comp_name, slug in slugs.items():
        specs.append(
            AssetSpec(
                asset_type="cad_projection",
                label=f"Render projections of {comp_name}",
                description=(
                    "Returns four signed image URLs (front, top, side, iso) "
                    "for the named component. Size is configurable."
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
                            "description": "long edge in pixels",
                        }
                    },
                    "additionalProperties": False,
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


class CadGltfParser(BaseParser):
    """Parse a .gltf or .glb assembly into markdown + per-component slugs.

    No baked images — renders are on-demand. The list of available
    components (with slugs) ships on ``ParserResult.on_demand_assets``
    so :func:`list_assets` can surface them later, and the same list
    appears in the markdown for searchability.
    """

    extensions: ClassVar[list[str]] = [".gltf", ".glb"]

    def parse(self, file_path: Path) -> ParserResult:
        # We deliberately don't use trimesh for tree extraction. trimesh
        # expands every NextAssemblyUsageOccurrence into a unique scene
        # graph node, which inflates the BOM (one pulley becomes "448×
        # Bearing" because trimesh counts every leaf-of-leaf-of-leaf
        # rather than the parts as the assembly file actually lists
        # them). The raw glTF JSON ``nodes`` array carries one entry
        # per FreeCAD-side part instance — exactly what we want.
        try:
            tree = _read_gltf_node_tree(file_path)
        except Exception as e:
            return ParserResult.failure(f"glTF parse failed: {e}")

        components = extract_components(tree.node_names)
        if not components:
            content = (
                f"# {file_path.stem}\n\n"
                f"CAD assembly · 0 top-level components (no routable nodes)"
            )
            return ParserResult(content=content)

        content, slugs = build_markdown(
            file_stem=file_path.stem,
            components=components,
            triangle_count=tree.triangle_count,
            generator=tree.generator,
        )
        return ParserResult(
            content=content,
            on_demand_assets=_emit_asset_specs(slugs),
        )


@dataclass
class _GltfTree:
    """Minimal subset of a parsed glTF for our parser. Avoids importing
    trimesh just to walk the node tree."""

    node_names: list[str]
    triangle_count: int
    generator: str | None


def _read_gltf_node_tree(file_path: Path) -> "_GltfTree":
    """Pull node names + triangle count from a glTF file.

    Handles both text glTF (the JSON document we can parse directly)
    and binary glb (a small header + the same JSON chunk). We don't
    need the meshes or buffers — only the names and the accessor
    sizes for the triangle tally.

    For binary glb, the layout is described at
    https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#glb-file-format-specification
    — first 12 bytes are header, then chunk 0 is the JSON.
    """
    import json
    import struct

    suffix = file_path.suffix.lower()
    if suffix == ".gltf":
        with open(file_path, "rb") as f:
            data = f.read()
        gltf = json.loads(data)
    elif suffix == ".glb":
        with open(file_path, "rb") as f:
            header = f.read(12)
            if len(header) < 12:
                raise ValueError("glb truncated header")
            magic, version, _length = struct.unpack("<III", header)
            if magic != 0x46546C67:  # 'glTF'
                raise ValueError(f"bad glb magic: {magic:#x}")
            if version != 2:
                raise ValueError(f"unsupported glb version: {version}")
            chunk0_header = f.read(8)
            chunk_length, chunk_type = struct.unpack("<II", chunk0_header)
            if chunk_type != 0x4E4F534A:  # 'JSON'
                raise ValueError(f"first glb chunk not JSON: {chunk_type:#x}")
            gltf = json.loads(f.read(chunk_length))
    else:
        raise ValueError(f"unsupported extension: {suffix!r}")

    if not isinstance(gltf, dict):
        raise ValueError("glTF root is not an object")

    nodes = gltf.get("nodes") or []
    node_names: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nm = n.get("name")
        if isinstance(nm, str) and nm:
            node_names.append(nm)

    triangle_count = _count_triangles(gltf)

    asset = gltf.get("asset") or {}
    generator = asset.get("generator") if isinstance(asset, dict) else None
    if generator is not None and not isinstance(generator, str):
        generator = None

    return _GltfTree(
        node_names=node_names,
        triangle_count=triangle_count,
        generator=generator,
    )


def _count_triangles(gltf: dict) -> int:
    """Sum the triangle count across every mesh primitive.

    Each primitive has either an ``indices`` accessor (triangle-list
    indices, count divides by 3) or none, in which case the
    ``POSITION`` accessor's count divides by 3. Other primitive modes
    (lines, points) are rare in CAD output and we don't try to be
    clever about them — counting their indices as triangles is
    inaccurate but cheap and the worst case is "the lead-in says 1%
    too many triangles".
    """
    accessors = gltf.get("accessors") or []
    meshes = gltf.get("meshes") or []
    total = 0
    for m in meshes:
        if not isinstance(m, dict):
            continue
        for prim in m.get("primitives") or []:
            if not isinstance(prim, dict):
                continue
            idx = prim.get("indices")
            if isinstance(idx, int) and 0 <= idx < len(accessors):
                cnt = accessors[idx].get("count") if isinstance(accessors[idx], dict) else None
                if isinstance(cnt, int):
                    total += cnt // 3
                continue
            attrs = prim.get("attributes") or {}
            pos = attrs.get("POSITION")
            if isinstance(pos, int) and 0 <= pos < len(accessors):
                cnt = accessors[pos].get("count") if isinstance(accessors[pos], dict) else None
                if isinstance(cnt, int):
                    total += cnt // 3
    return total
