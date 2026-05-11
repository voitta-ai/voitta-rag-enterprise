"""Tests for the STEP CAD parser.

We can't easily ship a binary STEP fixture (and committing a real
file means committing a CAD project's geometry too — not worth it).
Instead each test synthesizes a minimal STEP via OCP's
``STEPControl_Writer`` from a primitive geometry built in-memory.

Tests are skipped automatically if ``cadquery-ocp`` is not installed
locally; the CI image and the deploy box both have it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Skip the whole module if OCP isn't available — keeps the suite green
# on a contributor's laptop without the ~500 MB dependency.
ocp = pytest.importorskip("OCP")

from voitta_rag_enterprise.services.parsers.cad_step_parser import (  # noqa: E402
    CadStepParser,
    _bucket_labels,
    _clean_part_name,
    _route_name,
    _slugify,
)


# ---------------------------------------------------------------------------
# Pure-function tests — no OCP runtime needed
# ---------------------------------------------------------------------------


def test_route_name_double_colon() -> None:
    g, p = _route_name("Runway A :: p400")
    assert g == "Runway A"
    assert p == "p400"


def test_route_name_bracket_group() -> None:
    g, p = _route_name("Bearing 60206 [Floor pulley RR-a]")
    assert g == "Floor pulley RR-a"
    assert p == "Bearing 60206"


def test_route_name_unrouted_returns_fallback() -> None:
    """Anything that doesn't match either convention returns
    ``(None, name)`` — the caller buckets that under Unclassified
    rather than dropping it."""
    g, p = _route_name("Assembly 01-153.02.402")
    assert g is None
    assert p == "Assembly 01-153.02.402"


def test_route_name_skips_origin_markers() -> None:
    """FreeCAD-exported STEP files often contain ``OriginNNN`` nodes
    for the per-body coordinate system. They carry no geometry."""
    g, p = _route_name("Origin042")
    assert g is None and p is None


def test_clean_part_name_strips_instance_counter() -> None:
    """``Bearing 60206 (GOST 7242)003`` → ``Bearing 60206 (GOST 7242)``
    (the trailing 003 is a FreeCAD per-instance counter)."""
    assert _clean_part_name("Bearing 60206 (GOST 7242)003") == "Bearing 60206 (GOST 7242)"


def test_clean_part_name_keeps_space_separated_numbers() -> None:
    """``Bearing 60206`` (no instance suffix) must NOT be mangled —
    60206 is the real part number, not a counter."""
    assert _clean_part_name("Bearing 60206") == "Bearing 60206"


def test_slugify_collision_resolution() -> None:
    seen: set[str] = set()
    assert _slugify("Foo Bar", seen) == "foo-bar"
    assert _slugify("Foo Bar", seen) == "foo-bar-2"
    assert _slugify("Foo  Bar!!", seen) == "foo-bar-3"


# ---------------------------------------------------------------------------
# Routing — uses a fake shape_tool stub to drive _bucket_labels
# ---------------------------------------------------------------------------


class _FakeLabel:
    """Stand-in for a TDF_Label that just knows its name + entry. The
    parser code reads labels through ``_label_name`` / ``_label_entry``
    helpers; we monkey-patch those to avoid pulling in OCP for pure
    routing tests."""

    def __init__(self, name: str, entry: str = "0:1:1:1") -> None:
        self.name = name
        self.entry = entry


def test_bucket_labels_aggregates_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 bearings with the same name and a circlip in the same group
    must collapse into a ``hardware`` Counter with the right totals.
    The fabricated sheave shows up under ``parts`` exactly once."""
    from voitta_rag_enterprise.services.parsers import cad_step_parser

    names = [
        "Floor pulley RR-a :: sheave [101]",
        "Bearing 60206 (GOST 7242)001 [Floor pulley RR-a]",
        "Bearing 60206 (GOST 7242)002 [Floor pulley RR-a]",
        "Bearing 60206 (GOST 7242)003 [Floor pulley RR-a]",
        "Circlip DIN 472 62x2 [Floor pulley RR-a]",
    ]
    labels = [_FakeLabel(n, f"0:1:1:{i+1}") for i, n in enumerate(names)]

    monkeypatch.setattr(cad_step_parser, "_label_name", lambda lab: lab.name)
    monkeypatch.setattr(cad_step_parser, "_label_entry", lambda lab: lab.entry)
    monkeypatch.setattr(
        cad_step_parser, "_walk_tree", lambda st, lab, depth=0: [(lab, lab.name)]
    )

    class _Tools:
        @staticmethod
        def IsAssembly_s(_label):
            return False
    # Simulate a label sequence with the .Length()/.Value() API the
    # parser uses.

    class _Seq:
        def __init__(self, items): self._items = items
        def Length(self): return len(self._items)
        def Value(self, i): return self._items[i - 1]

    comps = _bucket_labels(_Tools(), _Seq(labels))
    assert "Floor pulley RR-a" in comps
    c = comps["Floor pulley RR-a"]
    assert c.parts == ["sheave [101]"]  # fabricated, unique
    assert c.hardware["Bearing 60206 (GOST 7242)"] == 3
    assert c.hardware["Circlip DIN 472 62x2"] == 1
    # Entry paths preserved for the render handler
    assert len(c.entry_paths) == 5


def test_bucket_labels_routes_double_colon_with_bracket_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Floor pulley :: sheave [101]`` matches BOTH regexes. The
    ``::`` reading must win: the part is the fabricated ``sheave
    [101]`` (an inventory tag in brackets), not a hardware bucket
    named ``101``."""
    from voitta_rag_enterprise.services.parsers import cad_step_parser

    monkeypatch.setattr(cad_step_parser, "_label_name", lambda lab: lab.name)
    monkeypatch.setattr(cad_step_parser, "_label_entry", lambda lab: lab.entry)
    monkeypatch.setattr(
        cad_step_parser, "_walk_tree", lambda st, lab, depth=0: [(lab, lab.name)]
    )

    class _Seq:
        def __init__(self, items): self._items = items
        def Length(self): return len(self._items)
        def Value(self, i): return self._items[i - 1]

    labels = [_FakeLabel("Floor pulley :: sheave [101]")]
    comps = _bucket_labels(None, _Seq(labels))
    assert "Floor pulley" in comps
    assert comps["Floor pulley"].parts == ["sheave [101]"]
    assert not comps["Floor pulley"].hardware


# ---------------------------------------------------------------------------
# End-to-end parser on a real synthesized STEP
# ---------------------------------------------------------------------------


def _write_minimal_step(path: Path) -> None:
    """Build a tiny STEP file in memory: a single named cube.

    Synthesizing the STEP via OCP rather than committing a binary
    fixture keeps the test repo lean and ensures the file matches the
    OCP version the rest of the deploy uses."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    writer = STEPControl_Writer()
    writer.Transfer(box, STEPControl_AsIs)
    status = writer.Write(str(path))
    assert status == IFSelect_ReturnStatus.IFSelect_RetDone, f"write status {status}"


def test_parser_handles_minimal_step(tmp_path: Path) -> None:
    """Round-trip: synthesize a tiny cube STEP, run the parser, expect
    success + at least one component + the asset menu."""
    step_path = tmp_path / "cube.step"
    _write_minimal_step(step_path)

    p = CadStepParser()
    r = p.parse(step_path)
    assert r.success, r.error
    assert "# cube" in r.content
    # The cube has no name attribute, so it lands in Unclassified —
    # still produces a renderable AssetSpec so the LLM can ask for it.
    assert len(r.on_demand_assets) >= 1
    spec = r.on_demand_assets[0]
    assert spec.asset_type == "cad_projection"
    # The bulletproof handle for the render path: at least one entry
    # path stored on the spec.
    assert spec.params_schema.get("x-entry-paths"), spec.params_schema


def test_parser_failure_on_bogus_step(tmp_path: Path) -> None:
    """Anything OCP rejects produces a ``ParserResult.failure`` rather
    than throwing, so the indexer marks the file as ``state='error'``
    instead of the whole extract crashing."""
    bad = tmp_path / "not_a_step.step"
    bad.write_text("ISO-10303-21;\nHEADER;\nthis isn't a real step file\n")
    r = CadStepParser().parse(bad)
    assert not r.success
    assert "STEP parse failed" in r.error


def test_parser_extracts_schema_from_step_header(tmp_path: Path) -> None:
    """The markdown lead-in surfaces the STEP application protocol
    (AP203/AP214/AP242). We grep it from the file's HEADER block."""
    step_path = tmp_path / "cube.step"
    _write_minimal_step(step_path)
    r = CadStepParser().parse(step_path)
    # AP214 / AP242 / AP203 — whichever OCP defaults to. Just verify
    # *some* schema string made it into the markdown.
    schema_match = re.search(r"Schema: (\S+)", r.content)
    assert schema_match, f"no Schema: in markdown:\n{r.content[:300]}"


def test_parser_extensions_include_stp(tmp_path: Path) -> None:
    """STEP files routinely arrive with ``.stp`` (older convention)
    instead of ``.step``. Both must dispatch."""
    p = CadStepParser()
    assert p.can_parse(Path("/tmp/x.step"))
    assert p.can_parse(Path("/tmp/x.stp"))
    assert not p.can_parse(Path("/tmp/x.gltf"))
