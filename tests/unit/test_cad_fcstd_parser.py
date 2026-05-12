"""Unit tests for the FCStd CAD parser.

FCStd is a zip; we synthesize minimal valid documents in-memory so
tests are self-contained (no committed binary fixtures). The OCP
dependency is only needed for the rendering path, NOT the parser —
the parser reads XML and treats brp blobs as opaque names. So these
tests run on a stock CI without cadquery-ocp.
"""

from __future__ import annotations

import math
import zipfile
from pathlib import Path
from textwrap import dedent

from voitta_rag_enterprise.services.parsers.cad_fcstd_parser import (
    CadFCStdParser,
    _matmul4,
    _parse_document_xml,
    _placement_to_matrix,
    _route_label,
    _slugify,
    _world_transform,
)


# ---------------------------------------------------------------------------
# Synthetic FCStd builder — produces a real zip that the parser can read
# ---------------------------------------------------------------------------


def _make_fcstd(tmp_path: Path, objects: list[dict]) -> Path:
    """Build an FCStd zip from a list of object dicts.

    Each dict is ``{"name", "type", "label", "placement"?, "group"?,
    "notes"?, "cells"?}`` where:
    - ``placement = (px, py, pz, q0, q1, q2, q3)``
    - ``group`` is a list of child object names (for App::Part containers)
    - ``notes`` is a ``{prop_name: value}`` mapping emitted as
      ``App::PropertyString`` annotations (Description / Note / etc.)
    - ``cells`` is a ``{address: content}`` mapping emitted as a
      Spreadsheet::Sheet ``cells`` property with one ``<Cell/>``
      element per entry, mirroring FreeCAD's PropertySheet::Save format
    """
    from xml.sax.saxutils import escape, quoteattr
    objs_xml = []
    data_xml = []
    for o in objects:
        objs_xml.append(f'    <Object type="{o["type"]}" name="{o["name"]}"/>')
        props = [
            f'        <Property name="Label" type="App::PropertyString">'
            f'<String value="{o.get("label", o["name"])}"/></Property>'
        ]
        if "placement" in o:
            px, py, pz, q0, q1, q2, q3 = o["placement"]
            props.append(
                f'        <Property name="Placement" type="App::PropertyPlacement">'
                f'<PropertyPlacement Px="{px}" Py="{py}" Pz="{pz}" '
                f'Q0="{q0}" Q1="{q1}" Q2="{q2}" Q3="{q3}" A="0" Ox="0" Oy="0" Oz="1"/>'
                f'</Property>'
            )
        if o.get("group"):
            links = "".join(f'<Link value="{c}"/>' for c in o["group"])
            props.append(
                f'        <Property name="Group" type="App::PropertyLinkList">'
                f'<LinkList>{links}</LinkList></Property>'
            )
        for prop_name, value in (o.get("notes") or {}).items():
            props.append(
                f'        <Property name="{prop_name}" '
                f'type="App::PropertyString" group="Notes">'
                f'<String value={quoteattr(value)}/></Property>'
            )
        cells = o.get("cells") or {}
        if cells:
            cell_xml = "".join(
                f'<Cell address="{addr}" content={quoteattr(content)}/>'
                for addr, content in cells.items()
            )
            props.append(
                f'        <Property name="cells" type="Spreadsheet::PropertySheet">'
                f'<Cells Count="{len(cells)}" xlink="1">{cell_xml}</Cells>'
                f'</Property>'
            )
        data_xml.append(
            f'    <Object name="{o["name"]}">\n'
            f'      <Properties Count="{len(props)}">\n'
            + "\n".join(props)
            + "\n      </Properties>\n"
            f'    </Object>'
        )
    # Don't use textwrap.dedent: interpolated lines have varying
    # indentation that confuses dedent's common-prefix calculation,
    # producing text where ``<?xml`` is preceded by whitespace and
    # the XML parser then rejects the file.
    doc = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<Document SchemaVersion="4">\n'
        "  <Objects>\n"
        + "\n".join(objs_xml) + "\n"
        "  </Objects>\n"
        "  <ObjectData>\n"
        + "\n".join(data_xml) + "\n"
        "  </ObjectData>\n"
        "</Document>\n"
    )
    path = tmp_path / "fixture.FCStd"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Document.xml", doc)
        # Add empty brp blobs for every Part::Feature so the parser's
        # ``brp_set`` membership check succeeds.
        for o in objects:
            if o["type"] == "Part::Feature":
                z.writestr(f'{o["name"]}.Shape.brp', b"DBRep_DrawableShape\n")
    return path


# ---------------------------------------------------------------------------
# Routing / slug pure-function tests
# ---------------------------------------------------------------------------


def test_route_label_handles_double_colon() -> None:
    g, p = _route_label("Floor pulley RR-a :: sheave [101]")
    assert g is None  # fabricated — no bracket-group annotation
    assert p == "sheave [101]"


def test_route_label_handles_bracket_group() -> None:
    g, p = _route_label("Bearing 60206 (GOST 7242)003 [Floor pulley RR-a]")
    assert g == "Floor pulley RR-a"
    assert p == "Bearing 60206 (GOST 7242)"  # 003 stripped


def test_route_label_skips_origin() -> None:
    g, p = _route_label("Origin042")
    assert g is None and p is None


def test_slugify_collisions() -> None:
    seen: set[str] = set()
    assert _slugify("Runway A", seen) == "runway-a"
    assert _slugify("Runway A", seen) == "runway-a-2"


# ---------------------------------------------------------------------------
# Placement / matrix math
# ---------------------------------------------------------------------------


def test_placement_identity_quaternion_is_identity_matrix() -> None:
    """Identity quaternion (0,0,0,1) at origin → identity matrix."""
    m = _placement_to_matrix((0, 0, 0, 0, 0, 0, 1))
    assert m[0] == 1 and m[5] == 1 and m[10] == 1 and m[15] == 1
    # No translation
    assert m[3] == 0 and m[7] == 0 and m[11] == 0


def test_placement_pure_translation() -> None:
    m = _placement_to_matrix((10, 20, 30, 0, 0, 0, 1))
    assert m[3] == 10 and m[7] == 20 and m[11] == 30


def test_matmul4_identity_left_and_right() -> None:
    ident = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    m = (
        2.0, 0.0, 0.0, 5.0,
        0.0, 3.0, 0.0, 6.0,
        0.0, 0.0, 4.0, 7.0,
        0.0, 0.0, 0.0, 1.0,
    )
    assert _matmul4(ident, m) == m
    assert _matmul4(m, ident) == m


# ---------------------------------------------------------------------------
# Document.xml parsing
# ---------------------------------------------------------------------------


def test_parse_document_xml_extracts_label_and_placement() -> None:
    """Round-trip a minimal doc through the XML parser and check that
    labels, placements, and parent-child links survive."""
    raw = dedent("""\
        <?xml version='1.0' encoding='utf-8'?>
        <Document>
          <Objects>
            <Object type="App::Part" name="root"/>
            <Object type="Part::Feature" name="leaf"/>
          </Objects>
          <ObjectData>
            <Object name="root">
              <Properties Count="2">
                <Property name="Label" type="App::PropertyString"><String value="Root Container"/></Property>
                <Property name="Group" type="App::PropertyLinkList"><LinkList><Link value="leaf"/></LinkList></Property>
              </Properties>
            </Object>
            <Object name="leaf">
              <Properties Count="2">
                <Property name="Label" type="App::PropertyString"><String value="My Leaf"/></Property>
                <Property name="Placement" type="App::PropertyPlacement">
                  <PropertyPlacement Px="10" Py="20" Pz="30" Q0="0" Q1="0" Q2="0" Q3="1" A="0" Ox="0" Oy="0" Oz="1"/>
                </Property>
              </Properties>
            </Object>
          </ObjectData>
        </Document>
    """).encode("utf-8")
    by_name = _parse_document_xml(raw)
    assert set(by_name) == {"root", "leaf"}
    assert by_name["leaf"].parent == "root"
    assert by_name["leaf"].label == "My Leaf"
    assert by_name["leaf"].placement[:3] == (10.0, 20.0, 30.0)
    assert by_name["root"].children == ["leaf"]


# ---------------------------------------------------------------------------
# End-to-end parser
# ---------------------------------------------------------------------------


def test_parser_emits_top_level_components(tmp_path: Path) -> None:
    """A FCStd with three App::Parts (one wrapper containing two
    children) yields all three as renderable components — including
    the wrapper, because the user may want to render everything."""
    fp = _make_fcstd(tmp_path, [
        {
            "name": "wrapper", "type": "App::Part", "label": "4-Post Lift",
            "group": ["runway_a", "runway_b"],
        },
        {
            "name": "runway_a", "type": "App::Part", "label": "Runway A",
            "group": ["pa"], "placement": (100, 0, 0, 0, 0, 0, 1),
        },
        {
            "name": "runway_b", "type": "App::Part", "label": "Runway B",
            "group": ["pb"], "placement": (200, 0, 0, 0, 0, 0, 1),
        },
        {"name": "pa", "type": "Part::Feature", "label": "Runway A :: p1"},
        {"name": "pb", "type": "Part::Feature", "label": "Runway B :: p1"},
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success, r.error
    slugs = {s.slug for s in r.on_demand_assets}
    assert "4-post-lift" in slugs
    assert "runway-a" in slugs
    assert "runway-b" in slugs
    # Single root → no synthetic Whole assembly
    assert "whole-assembly" not in slugs


def test_parser_handles_orphan_part_features(tmp_path: Path) -> None:
    """Part::Features not under any App::Part land in Unclassified."""
    fp = _make_fcstd(tmp_path, [
        {"name": "loose", "type": "Part::Feature", "label": "Loose part"},
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success
    slugs = {s.slug for s in r.on_demand_assets}
    assert "unclassified" in slugs


def test_parser_failure_on_non_zip(tmp_path: Path) -> None:
    fp = tmp_path / "not_a_zip.FCStd"
    fp.write_text("definitely not a zip")
    r = CadFCStdParser().parse(fp)
    assert not r.success
    assert "not a valid zip" in r.error.lower() or "fcstd" in r.error.lower()


def test_parser_failure_on_missing_document_xml(tmp_path: Path) -> None:
    fp = tmp_path / "empty.FCStd"
    with zipfile.ZipFile(fp, "w") as z:
        z.writestr("not_the_doc.xml", "<x/>")
    r = CadFCStdParser().parse(fp)
    assert not r.success
    assert "Document.xml" in r.error


def test_parser_carries_brp_members_on_assetspecs(tmp_path: Path) -> None:
    """Each AssetSpec carries the brp-member list under the
    ``x-fcstd-members`` schema key as ``[{"brp": "..."}]`` — no
    transforms. Render path composes transforms from a fresh
    Document.xml parse on every request."""
    fp = _make_fcstd(tmp_path, [
        {
            "name": "root", "type": "App::Part", "label": "Comp",
            "group": ["f1"], "placement": (1, 2, 3, 0, 0, 0, 1),
        },
        {
            "name": "f1", "type": "Part::Feature", "label": "f1",
            "placement": (10, 20, 30, 0, 0, 0, 1),
        },
    ])
    r = CadFCStdParser().parse(fp)
    spec = next(s for s in r.on_demand_assets if s.slug == "comp")
    members = spec.params_schema["x-fcstd-members"]
    assert len(members) == 1
    assert members[0] == {"brp": "f1.Shape.brp"}
    assert "transform" not in members[0]


def test_world_transform_excludes_leaf_placement() -> None:
    """``_world_transform`` returns the parent-chain product only.
    Leaf Placement is excluded because FreeCAD bakes it into the
    saved shape's OCC Location (``Feature::onChanged`` →
    ``shape.setTransform(Placement.toMatrix())``); including it here
    would double-apply at render time when
    ``BRepBuilderAPI_Transform(shape, T, copy=True)`` composes T
    over the shape's existing Location."""
    raw = dedent("""\
        <?xml version='1.0' encoding='utf-8'?>
        <Document>
          <Objects>
            <Object type="App::Part" name="root"/>
            <Object type="App::Part" name="mid"/>
            <Object type="Part::Feature" name="leaf"/>
          </Objects>
          <ObjectData>
            <Object name="root">
              <Properties Count="2">
                <Property name="Placement" type="App::PropertyPlacement"><PropertyPlacement Px="1" Py="2" Pz="3" Q0="0" Q1="0" Q2="0" Q3="1"/></Property>
                <Property name="Group" type="App::PropertyLinkList"><LinkList><Link value="mid"/></LinkList></Property>
              </Properties>
            </Object>
            <Object name="mid">
              <Properties Count="2">
                <Property name="Placement" type="App::PropertyPlacement"><PropertyPlacement Px="100" Py="200" Pz="300" Q0="0" Q1="0" Q2="0" Q3="1"/></Property>
                <Property name="Group" type="App::PropertyLinkList"><LinkList><Link value="leaf"/></LinkList></Property>
              </Properties>
            </Object>
            <Object name="leaf">
              <Properties Count="1">
                <Property name="Placement" type="App::PropertyPlacement"><PropertyPlacement Px="9999" Py="9999" Pz="9999" Q0="0" Q1="0" Q2="0" Q3="1"/></Property>
              </Properties>
            </Object>
          </ObjectData>
        </Document>
    """).encode("utf-8")
    by_name = _parse_document_xml(raw)
    tf = _world_transform(by_name, "leaf")
    # Parent chain only: root * mid translation = (1+100, 2+200, 3+300).
    # Leaf's 9999 placement MUST NOT leak in.
    assert math.isclose(tf[3], 101.0)
    assert math.isclose(tf[7], 202.0)
    assert math.isclose(tf[11], 303.0)


def test_world_transform_unparented_leaf_is_identity() -> None:
    """A leaf with no App::Part parent gets identity — its own
    Placement is carried by the shape's OCC Location."""
    raw = dedent("""\
        <?xml version='1.0' encoding='utf-8'?>
        <Document>
          <Objects>
            <Object type="Part::Feature" name="orphan"/>
          </Objects>
          <ObjectData>
            <Object name="orphan">
              <Properties Count="1">
                <Property name="Placement" type="App::PropertyPlacement"><PropertyPlacement Px="50" Py="60" Pz="70" Q0="0" Q1="0" Q2="0" Q3="1"/></Property>
              </Properties>
            </Object>
          </ObjectData>
        </Document>
    """).encode("utf-8")
    by_name = _parse_document_xml(raw)
    tf = _world_transform(by_name, "orphan")
    # Identity: 1 on the diagonal, 0 translation.
    assert tf[0] == 1.0 and tf[5] == 1.0 and tf[10] == 1.0
    assert tf[3] == 0.0 and tf[7] == 0.0 and tf[11] == 0.0


def test_parser_extension_matches() -> None:
    p = CadFCStdParser()
    assert p.can_parse(Path("/tmp/foo.fcstd"))
    assert p.can_parse(Path("/tmp/foo.FCStd"))
    assert not p.can_parse(Path("/tmp/foo.step"))


# ---------------------------------------------------------------------------
# Engineering-note + spreadsheet capture
# ---------------------------------------------------------------------------


def test_parser_captures_part_description(tmp_path: Path) -> None:
    """``App::PropertyString`` named ``Description`` (or any of the
    accepted aliases) attached to a Part::Feature lands in the
    component's Engineering notes section."""
    fp = _make_fcstd(tmp_path, [
        {
            "name": "root", "type": "App::Part", "label": "Lift",
            "group": ["bracket"],
        },
        {
            "name": "bracket", "type": "Part::Feature",
            "label": "Floor pulley RR-a :: sheave",
            "notes": {"Description": "Coincident with guide bar #4"},
        },
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success, r.error
    assert "### Engineering notes (1)" in r.content
    assert "Coincident with guide bar #4" in r.content
    assert "Floor pulley RR-a :: sheave" in r.content
    assert "_Description_" in r.content


def test_parser_captures_note_aliases(tmp_path: Path) -> None:
    """``Note``, ``Comment`` and ``Remark`` are all surfaced — authors
    don't always use the canonical ``Description`` spelling."""
    fp = _make_fcstd(tmp_path, [
        {
            "name": "root", "type": "App::Part", "label": "Lift",
            "group": ["a", "b", "c"],
        },
        {"name": "a", "type": "Part::Feature", "label": "Part A",
         "notes": {"Note": "TBD: revise tolerance"}},
        {"name": "b", "type": "Part::Feature", "label": "Part B",
         "notes": {"Comment": "Vendor change pending"}},
        {"name": "c", "type": "Part::Feature", "label": "Part C",
         "notes": {"Remarks": "Re-check at next review"}},
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success
    assert "TBD: revise tolerance" in r.content
    assert "Vendor change pending" in r.content
    assert "Re-check at next review" in r.content


def test_parser_skips_empty_descriptions(tmp_path: Path) -> None:
    """An empty Description property doesn't create a Notes section."""
    fp = _make_fcstd(tmp_path, [
        {
            "name": "root", "type": "App::Part", "label": "Lift",
            "group": ["a"],
        },
        {"name": "a", "type": "Part::Feature", "label": "Part A",
         "notes": {"Description": ""}},
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success
    assert "Engineering notes" not in r.content


def test_parser_captures_spreadsheet_cells(tmp_path: Path) -> None:
    """``Spreadsheet::Sheet`` cells round-trip into the markdown as
    address-keyed rows, sorted row-then-column."""
    fp = _make_fcstd(tmp_path, [
        {
            "name": "root", "type": "App::Part", "label": "Lift",
            "group": ["leaf"],
        },
        {"name": "leaf", "type": "Part::Feature", "label": "Bracket"},
        {
            "name": "EngineeringNotes",
            "type": "Spreadsheet::Sheet",
            "label": "Engineering Notes",
            "cells": {
                "A1": "Label",
                "B1": "Status",
                "C1": "Note",
                "A2": "Bracket",
                "B2": "TBD",
                "C2": "Awaiting drawings",
                "A3": "Pulley",
                "B3": "OK",
                "C3": "Verified 2026-04",
            },
        },
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success, r.error
    assert "## Spreadsheets" in r.content
    assert "### Sheet: Engineering Notes" in r.content
    assert "`A1`: Label" in r.content
    assert "`C3`: Verified 2026-04" in r.content
    # Row-major order: header row, then row 2, then row 3.
    idx_a1 = r.content.index("`A1`")
    idx_b1 = r.content.index("`B1`")
    idx_a2 = r.content.index("`A2`")
    assert idx_a1 < idx_b1 < idx_a2


def test_parser_drops_empty_sheets(tmp_path: Path) -> None:
    """A Spreadsheet object with no cells doesn't emit a section."""
    fp = _make_fcstd(tmp_path, [
        {"name": "root", "type": "App::Part", "label": "Lift", "group": ["leaf"]},
        {"name": "leaf", "type": "Part::Feature", "label": "Bracket"},
        {"name": "EmptySheet", "type": "Spreadsheet::Sheet", "label": "Empty"},
    ])
    r = CadFCStdParser().parse(fp)
    assert r.success
    assert "## Spreadsheets" not in r.content
