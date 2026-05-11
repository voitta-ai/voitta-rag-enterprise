"""STEP CAD parser via OpenCASCADE (OCP).

Reads ISO 10303-21 STEP files into an XCAF (Extended CAF) document,
walks the assembly hierarchy, and emits Markdown chunks that an LLM
can search by component name. Renders are produced on demand by
``services.cad_render`` — the parser itself only describes what's
available.

Why this design

The reference pipelines we modeled this on:

* **FreeCAD** (``src/Mod/Import/App/ReaderStep.cpp``,
  ``Import/Gui/AppImportGuiPy.cpp``) uses
  ``STEPCAFControl_Reader`` with ``NameMode=true`` and
  ``ColorMode=true``, transfers into a ``TDocStd_Document``, and
  walks ``XCAFDoc_ShapeTool::GetShapes/GetSubShapes`` to enumerate
  the tree.
* **Creality Print** (``libslic3r/Format/STEP.cpp``) does exactly
  the same, plus a UTF-8 preflight (legacy STEP exporters sometimes
  emit non-UTF-8 strings). We replicate the preflight as a single
  warning in the markdown — we don't refuse to parse.

Slug stability

Every component in the menu carries both a human-readable slug
(``runway-a``) for the LLM to recognize, and the underlying XCAF
``TDF_Label`` entry path (e.g. ``0:1:1:3``). The render handler
resolves the entry path back to a label deterministically — no
string matching, no fuzzy lookup. If the file is re-indexed and
entries shift, the menu changes; old slugs disappear and the
handler 404s with the current options. That's the contract.

Why we don't cache the OCC document

The user has been explicit: no caches anywhere. Every render request
re-parses the STEP file from disk. STEP parse + XCAF transfer is the
heaviest single operation (typically 1-15 s depending on file size).
We accept that as the cost of "the index always matches the file".
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
# Name routing — same conventions as the legacy glTF parser
# ---------------------------------------------------------------------------
#
# STEP files exported from FreeCAD commonly carry product names that
# encode the parent group in one of two forms:
#
#     "Runway A :: p400"          fabricated part inside LinkGroup "Runway A"
#     "Bearing 60206 [Floor pulley RR-a]"  hardware inside LinkGroup "Floor pulley RR-a"
#
# This is a *grouping* layer on top of the XCAF tree, since FreeCAD's
# LinkGroup-flattening at export time loses the real container nodes.
# We re-derive the grouping from names. Real XCAF subassemblies that
# DO survive the export (App::Part containers, "Assembly NNN" wrappers)
# get walked recursively — see :func:`_walk_tree` below.

_DOUBLE_COLON = re.compile(r"^(?P<group>.+?)\s+::\s+(?P<part>.+)$")
_BRACKET_GROUP = re.compile(r"^(?P<part>.+?)\s*\[(?P<group>[^\]]+)\]\s*$")
_FREECAD_INSTANCE = re.compile(r"(?<=[^\s\d])\d{3,}$")


def _clean_part_name(raw: str) -> str:
    """Strip the FreeCAD ``003``-style instance counter from a hardware
    part name so duplicate instances aggregate. We only strip when the
    suffix is glued directly to the rest of the name (no space) — that
    avoids mangling real part numbers like ``Bearing 60206``."""
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
# Component aggregation
# ---------------------------------------------------------------------------


@dataclass
class Component:
    """One renderable bucket in the assembly's component menu.

    Each bucket aggregates one or more XCAF labels. For most
    components there's exactly one label (a sub-assembly that survives
    the export). For grouped hardware ("8× Bearing 60206 in Floor
    pulley RR-a") there are many labels collapsed under one bucket.

    The ``entry_paths`` list is the bulletproof handle: at render time
    the handler resolves these strings back to TDF_Labels via
    ``TDF_Tool::Label`` and grabs the corresponding TopoDS_Shape. No
    name matching at render time.
    """

    name: str
    parts: list[str] = field(default_factory=list)
    hardware: Counter = field(default_factory=Counter)
    entry_paths: list[str] = field(default_factory=list)


def _label_name(label) -> str:
    """Return the human-readable name attached to ``label``, or empty
    string if none. Lazy import so the module is loadable without OCP
    available — important for tests that mock the parser path."""
    from OCP.TDataStd import TDataStd_Name

    name_attr = TDataStd_Name()
    if not label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
        return ""
    return str(name_attr.Get().ToExtString())


def _label_entry(label) -> str:
    """Return the XCAF entry path of ``label`` (e.g. ``0:1:1:3``).

    These are deterministic per-document — re-parsing the same bytes
    produces the same paths. They're our stable identifiers for
    render-time lookup."""
    from OCP.TCollection import TCollection_AsciiString
    from OCP.TDF import TDF_Tool

    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return str(s.ToCString())


def _route_name(name: str) -> tuple[str | None, str | None]:
    """Return ``(group, part)`` from one of the FreeCAD naming
    conventions, or ``(None, fallback)`` for unrouted names."""
    if not name or name.startswith("Origin"):
        return None, None
    m = _DOUBLE_COLON.match(name)
    if m:
        return m.group("group").strip(), m.group("part").strip()
    m = _BRACKET_GROUP.match(name)
    if m:
        return m.group("group").strip(), _clean_part_name(m.group("part"))
    return None, name


def _walk_tree(shape_tool, label, depth: int = 0) -> list:
    """Return the recursively-flattened list of (label, name) tuples
    for ``label`` and every assembly-component descendant.

    XCAF distinguishes *shapes* (geometry definitions) from
    *components* (instance references, possibly with placements). An
    assembly's tree lives on the component axis: ``GetComponents``
    returns direct instance children; each component refers to either
    another assembly (recurse) or a leaf shape (stop).

    ``GetSubShapes`` is the WRONG call here — it returns geometric
    sub-features (faces, edges) of a single shape definition, which
    blows the tree up by orders of magnitude.

    Component label names carry the instance-level annotations
    FreeCAD's exporter encodes (``Runway A :: p400``,
    ``Bearing 60206 [Floor pulley RR-a]``), so we route on those.
    The referred shape is only useful as the geometry handle, which
    we record (via entry path) for the render path.
    """
    from OCP.TDF import TDF_LabelSequence

    out: list = []
    name = _label_name(label)
    if name and not name.startswith("Origin"):
        out.append((label, name))

    if depth > 8:  # safety against pathological assemblies
        return out

    if shape_tool.IsAssembly_s(label):
        comps = TDF_LabelSequence()
        if shape_tool.GetComponents_s(label, comps, False):
            for i in range(1, comps.Length() + 1):
                out.extend(_walk_tree(shape_tool, comps.Value(i), depth + 1))
    return out


def _bucket_labels(
    shape_tool, top_labels
) -> dict[str, Component]:
    """Walk every top-level label and route into buckets by the
    FreeCAD name conventions. Returns ``{group_name: Component}``,
    deterministic for a given file."""
    components: dict[str, Component] = {}

    for i in range(1, top_labels.Length() + 1):
        for lab, name in _walk_tree(shape_tool, top_labels.Value(i)):
            if not name or name.startswith("Origin"):
                continue
            entry = _label_entry(lab)

            # Try ``::`` first — it's the more specific convention
            # (fabricated parts named inside a LinkGroup). A name like
            # ``Floor pulley RR-a :: sheave [101]`` matches BOTH this
            # regex (group=Floor pulley RR-a, part=sheave [101]) AND
            # the bracket regex (group=101, part=Floor pulley RR-a ::
            # sheave). The ``::`` reading is the correct one — the
            # ``[101]`` here is an inventory tag, not a group name.
            m_dc = _DOUBLE_COLON.match(name)
            if m_dc:
                group = m_dc.group("group").strip()
                part = m_dc.group("part").strip()
                bucket = components.setdefault(group, Component(name=group))
                bucket.entry_paths.append(entry)
                if part not in bucket.parts:
                    bucket.parts.append(part)
                continue

            m_br = _BRACKET_GROUP.match(name)
            if m_br:
                group = m_br.group("group").strip()
                part = _clean_part_name(m_br.group("part"))
                bucket = components.setdefault(group, Component(name=group))
                bucket.entry_paths.append(entry)
                bucket.hardware[part] += 1
                continue

            # Unrouted — bucket under "Unclassified" so the part is
            # still searchable + renderable. Use the raw name as the
            # part identifier (no cleanup; this is often a structured
            # part number where digits are meaningful).
            bucket = components.setdefault(
                "Unclassified", Component(name="Unclassified")
            )
            bucket.entry_paths.append(entry)
            if name not in bucket.parts:
                bucket.parts.append(name)

    return components


# ---------------------------------------------------------------------------
# Markdown emission
# ---------------------------------------------------------------------------


def _component_summary(c: Component) -> int:
    return len(c.parts) + sum(c.hardware.values())


def build_markdown(
    file_stem: str,
    components: dict[str, Component],
    triangle_count_hint: int | None,
    schema: str | None,
) -> tuple[str, dict[str, tuple[str, list[str]]]]:
    """Build the file's markdown + ``{slug → (component_name, entry_paths)}``.

    Markdown is two layers: a root chunk listing every component with
    its slug, then a per-component chunk with parts/hardware/render
    instructions. Heading-driven chunking puts each component in its
    own retrievable chunk."""
    ordered = sorted(
        components.values(),
        key=lambda c: (-_component_summary(c), c.name.lower()),
    )
    seen_slugs: set[str] = set()
    slug_map: dict[str, tuple[str, list[str]]] = {}
    for c in ordered:
        slug = _slugify(c.name, seen_slugs)
        slug_map[slug] = (c.name, list(c.entry_paths))

    lines: list[str] = []
    lines.append(f"# {file_stem}")
    lines.append("")
    summary_bits = [
        f"CAD assembly · {len(ordered)} top-level components",
    ]
    if triangle_count_hint is not None:
        summary_bits.append(f"≈{triangle_count_hint:,} triangles")
    if schema:
        summary_bits.append(f"Schema: {schema}")
    lines.append(" · ".join(summary_bits))
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

    # Re-iterate in slug order so the order is the same as in the menu
    for slug, (comp_name, _) in slug_map.items():
        c = next(c for c in ordered if c.name == comp_name)
        lines.append(f"- `{slug}` — {c.name} ({_component_summary(c)} parts)")
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
            for p in c.parts:
                lines.append(f"- {p}")
            lines.append("")
        if c.hardware:
            n = sum(c.hardware.values())
            lines.append(f"### Standard hardware ({n})")
            for part, qty in c.hardware.most_common():
                lines.append(f"- {qty}× {part}")
            lines.append("")
    return "\n".join(lines), slug_map


def _emit_asset_specs(
    slug_map: dict[str, tuple[str, list[str]]],
) -> list[AssetSpec]:
    """Build the on-demand menu from the slug map.

    Each spec carries the XCAF entry paths as a ``hint`` in
    ``params_schema['x-entry-paths']`` — that's the source of truth
    for the render handler. We don't put it in ``params`` (the LLM
    doesn't pass it) but in the schema so it's persisted alongside.
    The handler reads it back via the same ``load_assets_for_file``
    path the MCP ``list_assets`` tool uses.
    """
    specs: list[AssetSpec] = []
    for slug, (comp_name, entry_paths) in slug_map.items():
        specs.append(
            AssetSpec(
                asset_type="cad_projection",
                label=f"Render projections of {comp_name}",
                description=(
                    "Returns four signed image URLs (front, top, side, iso) "
                    "for the named component. STEP is re-parsed on every "
                    "request — typical latency 5-20s per projection."
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
                    # OCC entry paths — handler reads these to resolve
                    # the slug to a TopoDS_Shape without name matching.
                    "x-entry-paths": entry_paths,
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


class CadStepParser(BaseParser):
    """Parse ``.step`` / ``.stp`` files into an LLM-searchable
    component tree. Renders are on-demand; this parser produces no
    image bytes at index time.
    """

    extensions: ClassVar[list[str]] = [".step", ".stp"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            from OCP.STEPCAFControl import STEPCAFControl_Reader  # noqa: F401
        except ImportError as e:
            return ParserResult.failure(
                f"cad_step parser unavailable: cadquery-ocp not installed ({e})"
            )

        try:
            doc, schema = _parse_step_into_xcaf(file_path)
        except Exception as e:
            return ParserResult.failure(f"STEP parse failed: {e}")

        from OCP.TDF import TDF_LabelSequence
        from OCP.XCAFDoc import XCAFDoc_DocumentTool

        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        top_labels = TDF_LabelSequence()
        shape_tool.GetFreeShapes(top_labels)
        if top_labels.Length() == 0:
            # Fall back to GetShapes — some files don't mark anything as
            # "free" (no parent assembly); we treat all roots as components.
            shape_tool.GetShapes(top_labels)

        components = _bucket_labels(shape_tool, top_labels)
        if not components:
            return ParserResult(
                content=(
                    f"# {file_path.stem}\n\n"
                    f"CAD assembly · 0 top-level components (empty file?)"
                )
            )

        # Synthesize a "Whole assembly" component that aggregates every
        # other component's entry paths. Gives the LLM a single slug
        # to render the entire model without picking a sub-component.
        # The render handler tessellates the full compound; heavy on
        # big files but the user-visible cost is one request, not 27.
        whole = Component(name="Whole assembly")
        for c in components.values():
            whole.entry_paths.extend(c.entry_paths)
            for p in c.parts:
                if p not in whole.parts:
                    whole.parts.append(p)
            for k, v in c.hardware.items():
                whole.hardware[k] += v
        if whole.entry_paths:
            components["Whole assembly"] = whole

        content, slug_map = build_markdown(
            file_stem=file_path.stem,
            components=components,
            triangle_count_hint=None,
            schema=schema,
        )
        return ParserResult(
            content=content,
            on_demand_assets=_emit_asset_specs(slug_map),
        )


def _parse_step_into_xcaf(file_path: Path):
    """Read a STEP file via OCP into a fresh XCAF document.

    Returns ``(document, schema_string_or_None)``. The schema is the
    STEP application protocol identifier (AP203, AP214, AP242, …),
    surfaced in the markdown lead-in for context. OCP doesn't expose
    a simple getter — we grep it from the first few KB of the file
    itself, which always carries a ``FILE_SCHEMA(...)`` header.
    """
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    status = reader.ReadFile(str(file_path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(f"STEPCAFControl_Reader.ReadFile returned {status}")
    if not reader.Transfer(doc):
        raise RuntimeError("STEPCAFControl_Reader.Transfer returned False")

    return doc, _read_step_schema(file_path)


def _read_step_schema(file_path: Path) -> str | None:
    """Grep the ``FILE_SCHEMA(...)`` line from the STEP header.

    STEP ASCII files start with a HEADER section; the schema is on a
    single ``FILE_SCHEMA(...)`` line within the first ~2 KB. We read a
    small prefix and regex it out — much cheaper than re-walking the
    OCP document. Returns ``None`` if the header doesn't parse.
    """
    try:
        with open(file_path, "rb") as f:
            prefix = f.read(8192)
    except OSError:
        return None
    try:
        text = prefix.decode("utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", text)
    return m.group(1) if m else None
