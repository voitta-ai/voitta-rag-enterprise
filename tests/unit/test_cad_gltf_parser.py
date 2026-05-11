"""Unit tests for the glTF CAD parser — name routing, slug generation,
markdown emission, and the failure paths."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from voitta_rag_enterprise.services.parsers.cad_gltf_parser import (
    CadGltfParser,
    _clean_part_name,
    _route_name,
    _slugify,
    extract_components,
)


def _write_gltf(path: Path, nodes: list[dict], asset_extras: dict | None = None) -> None:
    """Minimal viable glTF: scene + nodes + accessors + meshes referencing
    a single fake position accessor. We don't need the buffer for our
    tests — we never call trimesh on these files; the parser reads the
    JSON directly."""
    payload = {
        "asset": {"version": "2.0", **(asset_extras or {})},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [{
            "primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]
        }],
        "accessors": [
            {"componentType": 5126, "count": 6, "type": "VEC3"},
            {"componentType": 5125, "count": 6, "type": "SCALAR"},
        ],
        "bufferViews": [],
        "buffers": [],
    }
    path.write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# _route_name
# ---------------------------------------------------------------------------


def test_route_name_double_colon_convention() -> None:
    g, p = _route_name("Runway A :: p400")
    assert g == "Runway A"
    assert p == "p400"


def test_route_name_bracket_convention() -> None:
    g, p = _route_name("Bearing 60206 (GOST 7242) [Floor pulley RR-a]")
    assert g == "Floor pulley RR-a"
    assert p == "Bearing 60206 (GOST 7242)"


def test_route_name_strips_instance_suffix() -> None:
    """FreeCAD adds ``003``-style instance counters to hardware names;
    they should disappear from the routed part name so duplicate
    hardware aggregates."""
    g, p = _route_name("Bearing 60206 (GOST 7242)003 [Floor pulley RR-a]")
    assert g == "Floor pulley RR-a"
    assert p == "Bearing 60206 (GOST 7242)"


def test_route_name_strips_trimesh_suffix() -> None:
    """trimesh re-suffixes instanced nodes with ``_<hex>`` — the
    routing pass must look past that to find the bracket group."""
    g, p = _route_name("Circlip DIN 472 62x2 [Floor pulley RR-a]_3_70847e")
    assert g == "Floor pulley RR-a"
    assert p == "Circlip DIN 472 62x2"


def test_route_name_skips_origin_nodes() -> None:
    g, p = _route_name("Origin042")
    assert g is None and p is None


def test_route_name_handles_unrouted() -> None:
    """A name without either convention gets returned as
    (None, cleaned_name) so the caller can bucket it under
    Unclassified — not dropped."""
    g, p = _route_name("Assembly 01-153.02.402")
    assert g is None
    assert p is not None


# ---------------------------------------------------------------------------
# extract_components
# ---------------------------------------------------------------------------


def test_extract_components_aggregates_hardware() -> None:
    names = [
        "Floor pulley RR-a :: sheave [101]",
        "Bearing 60206 (GOST 7242)001 [Floor pulley RR-a]",
        "Bearing 60206 (GOST 7242)002 [Floor pulley RR-a]",
        "Bearing 60206 (GOST 7242)003 [Floor pulley RR-a]",
        "Circlip DIN 472 62x2 [Floor pulley RR-a]",
    ]
    comps = extract_components(names)
    assert "Floor pulley RR-a" in comps
    c = comps["Floor pulley RR-a"]
    # Hardware: 3 bearings (same name after stripping suffix) → counted as 3
    assert c.hardware["Bearing 60206 (GOST 7242)"] == 3
    assert c.hardware["Circlip DIN 472 62x2"] == 1
    # Fabricated part listed once
    assert c.parts == ["sheave [101]"]


def test_extract_components_routes_two_groups_separately() -> None:
    names = [
        "Runway A :: p400",
        "Runway B :: p401",
        "Bearing 60206 [Runway A]",
    ]
    comps = extract_components(names)
    assert set(comps) == {"Runway A", "Runway B"}
    assert comps["Runway A"].parts == ["p400"]
    assert comps["Runway A"].hardware["Bearing 60206"] == 1
    assert comps["Runway B"].parts == ["p401"]


def test_extract_components_buckets_unrouted_into_unclassified() -> None:
    """An assembly name without ``::`` or ``[group]`` lands in the
    Unclassified bucket — still searchable via its part name."""
    comps = extract_components(["Assembly 01-153.02.402"])
    assert "Unclassified" in comps
    parts = comps["Unclassified"].parts
    assert any("01-153.02.402" in p for p in parts)


def test_extract_components_skips_origin_nodes() -> None:
    comps = extract_components(["Origin042", "Origin003"])
    assert comps == {}


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    seen: set[str] = set()
    assert _slugify("Floor pulley RR-a", seen) == "floor-pulley-rr-a"
    assert _slugify("Drive Unit", seen) == "drive-unit"


def test_slugify_resolves_collisions() -> None:
    seen: set[str] = set()
    assert _slugify("Foo Bar", seen) == "foo-bar"
    assert _slugify("Foo Bar", seen) == "foo-bar-2"
    assert _slugify("Foo Bar", seen) == "foo-bar-3"


def test_slugify_empty_fallback() -> None:
    seen: set[str] = set()
    assert _slugify("", seen) == "component"


def test_clean_part_name_handles_combined_suffixes() -> None:
    # FreeCAD ``003`` + trimesh ``_3_abcdef``
    assert _clean_part_name("Bearing X003_3_abcdef") == "Bearing X"


# ---------------------------------------------------------------------------
# Parser — end-to-end on a synthetic glTF
# ---------------------------------------------------------------------------


def test_parser_emits_root_chunk_and_per_component_chunks(tmp_path: Path) -> None:
    """End-to-end: a synthetic glTF with three nodes under two groups
    should produce a root chunk listing both components plus one
    ``## Component:`` chunk for each."""
    f = tmp_path / "fixture.gltf"
    _write_gltf(
        f,
        nodes=[
            {"name": "root", "children": [1, 2, 3]},
            {"name": "Group A :: p1", "mesh": 0},
            {"name": "Group A :: p2", "mesh": 0},
            {"name": "Bearing X [Group B]", "mesh": 0},
        ],
        asset_extras={"generator": "test"},
    )
    r = CadGltfParser().parse(f)
    assert r.success, r.error
    assert "# fixture" in r.content
    assert "## Components" in r.content
    assert "## Component: Group A" in r.content
    assert "## Component: Group B" in r.content
    assert "p1" in r.content
    assert "p2" in r.content
    assert "Bearing X" in r.content
    # Both components surfaced as on-demand assets.
    asset_types = {a.asset_type for a in r.on_demand_assets}
    assert asset_types == {"cad_projection"}
    slugs = {a.slug for a in r.on_demand_assets}
    assert "group-a" in slugs
    assert "group-b" in slugs


def test_parser_reads_glb_binary_format(tmp_path: Path) -> None:
    """Same JSON, packaged as binary glb — header + JSON chunk."""
    json_payload = json.dumps({
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {"name": "root", "children": [1]},
            {"name": "Runway A :: p400"},
        ],
        "meshes": [],
        "accessors": [],
    }).encode("utf-8")
    # glTF binary spec: pad JSON chunk to 4-byte alignment with spaces.
    while len(json_payload) % 4 != 0:
        json_payload += b" "
    chunk_header = struct.pack("<II", len(json_payload), 0x4E4F534A)
    total_length = 12 + 8 + len(json_payload)
    header = struct.pack("<III", 0x46546C67, 2, total_length)
    glb = header + chunk_header + json_payload

    f = tmp_path / "fixture.glb"
    f.write_bytes(glb)
    r = CadGltfParser().parse(f)
    assert r.success, r.error
    assert "Runway A" in r.content


def test_parser_failure_on_invalid_gltf(tmp_path: Path) -> None:
    f = tmp_path / "broken.gltf"
    f.write_text("not json at all")
    r = CadGltfParser().parse(f)
    assert not r.success
    assert "glTF parse failed" in r.error


def test_parser_failure_on_invalid_glb_magic(tmp_path: Path) -> None:
    f = tmp_path / "broken.glb"
    # Wrong magic header
    f.write_bytes(b"WRNG" + b"\x00" * 12)
    r = CadGltfParser().parse(f)
    assert not r.success


def test_parser_empty_file_produces_stub(tmp_path: Path) -> None:
    """A glTF with no routable nodes still produces a result — just a
    short stub markdown so the file is searchable by its name."""
    f = tmp_path / "blank.gltf"
    _write_gltf(f, nodes=[{"name": "Origin000"}])
    r = CadGltfParser().parse(f)
    assert r.success
    assert "blank" in r.content
