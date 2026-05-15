"""Tests for the CAD-aware chunker.

Verifies that every ``## Component:`` section in FCStd parser output
lands in its own chunk, so per-component retrieval works in search.
"""

from __future__ import annotations

from pathlib import Path

from voitta_rag_enterprise.services.chunking.cad import (
    MAX_CHARS,
    CadComponentStrategy,
)
from voitta_rag_enterprise.services.chunking.registry import (
    build_default_registry,
)


def _doc(components: list[tuple[str, str]]) -> str:
    """Build a minimal FCStd-style markdown document for the chunker."""
    lines = [
        "# lift",
        "",
        "FreeCAD assembly · 0 containers, 0 features, ...",
        "",
        "## Components",
        "",
        "- index entry",
        "",
    ]
    for slug, label in components:
        lines.append(f"## Component: {label}")
        lines.append("")
        lines.append(f"Slug: `{slug}`")
        lines.append('Renderable via:')
        lines.append(
            f'- `request_asset(file_id=<N>, asset_type="cad_mesh", slug="{slug}")`'
        )
        lines.append("")
    return "\n".join(lines)


def test_one_chunk_per_component_section() -> None:
    text = _doc([
        ("lift/frame", "Frame"),
        ("lift/frame/rail-l", "Rail L"),
        ("lift/frame/rail-r", "Rail R"),
    ])
    chunks = CadComponentStrategy().chunk(text, Path("fixture.FCStd"))
    # 1 preamble + 3 components
    assert len(chunks) == 4
    # Each component chunk contains exactly its own slug, not another's.
    assert "lift/frame/rail-l" in chunks[2].text
    assert "lift/frame/rail-r" not in chunks[2].text
    assert "lift/frame/rail-r" in chunks[3].text


def test_spreadsheets_section_is_its_own_chunk() -> None:
    text = (
        "# lift\n\n"
        "## Components\n\n- entry\n\n"
        "## Component: Frame\n\nSlug: `frame`\n\n"
        "## Spreadsheets\n\n### Sheet: Notes\n\n- `A1`: foo\n"
    )
    chunks = CadComponentStrategy().chunk(text, Path("fixture.FCStd"))
    assert len(chunks) == 3
    assert chunks[-1].text.startswith("## Spreadsheets")


def test_giant_section_falls_back_to_paragraph_split() -> None:
    # A component section with 4× MAX_CHARS of body — should get
    # paragraph-split rather than emitted as one mega-chunk.
    body = "Paragraph filler.\n\n" * (MAX_CHARS // 18 + 1)
    text = (
        "# lift\n\n## Components\n\n- entry\n\n"
        f"## Component: Big\n\nSlug: `big`\n\n{body}"
    )
    chunks = CadComponentStrategy().chunk(text, Path("fixture.FCStd"))
    assert all(len(c.text) <= MAX_CHARS for c in chunks)
    # Big section produces multiple chunks; preamble is still one chunk.
    assert len(chunks) >= 3


def test_offsets_match_original_text() -> None:
    text = _doc([("frame", "Frame"), ("frame/rail", "Rail")])
    chunks = CadComponentStrategy().chunk(text, Path("fixture.FCStd"))
    for c in chunks:
        assert text[c.char_start:c.char_end] == c.text


def test_registry_picks_cad_strategy_for_fcstd() -> None:
    r = build_default_registry()
    assert isinstance(r.find(Path("foo.FCStd")), CadComponentStrategy)
    assert isinstance(r.find(Path("foo.fcstd")), CadComponentStrategy)
    # Non-FCStd files still go to paragraph strategy (or code).
    assert not isinstance(r.find(Path("foo.md")), CadComponentStrategy)
