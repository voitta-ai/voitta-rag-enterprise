"""Cross-strategy invariants: round-trip slicing, ordering, coverage, budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from voitta_rag_enterprise.services.chunking import (
    ChunkInfo,
    build_default_registry,
)


PY_SRC = (
    "import os\n"
    "import sys\n\n"
    "def add(a, b):\n"
    "    return a + b\n\n"
    "class Box:\n"
    "    def __init__(self, v):\n"
    "        self.v = v\n\n"
    "    def get(self):\n"
    "        return self.v\n"
)

TS_SRC = (
    "import { foo } from 'bar';\n\n"
    "export function add(a: number, b: number): number {\n"
    "    return a + b;\n"
    "}\n\n"
    "export const PI = 3.14;\n"
)

GO_SRC = (
    "package main\n\nimport \"fmt\"\n\n"
    "func add(a int, b int) int {\n    return a + b\n}\n\n"
    "func main() {\n    fmt.Println(add(1, 2))\n}\n"
)

MD_SRC = (
    "# Title\n\n"
    + ("paragraph body line.\n" * 30)
    + "\n## Section\n\n"
    + ("more body.\n" * 30)
)

CASES = [
    pytest.param("file.py", PY_SRC, id="python"),
    pytest.param("file.ts", TS_SRC, id="typescript"),
    pytest.param("file.go", GO_SRC, id="go"),
    pytest.param("README.md", MD_SRC, id="markdown-paragraph"),
    pytest.param("notes.txt", MD_SRC, id="text-paragraph"),
]


def _chunk(name: str, text: str) -> list[ChunkInfo]:
    return build_default_registry().chunk(text, Path(name))


@pytest.mark.parametrize(("name", "src"), CASES)
def test_chunks_slice_back_to_source(name: str, src: str) -> None:
    """``chunk.text == source[char_start:char_end]`` for every chunk."""
    for c in _chunk(name, src):
        assert src[c.char_start : c.char_end] == c.text


@pytest.mark.parametrize(("name", "src"), CASES)
def test_chunks_are_ordered_by_char_start(name: str, src: str) -> None:
    chunks = _chunk(name, src)
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)


@pytest.mark.parametrize(("name", "src"), CASES)
def test_chunks_cover_full_file(name: str, src: str) -> None:
    """First chunk starts at 0, last ends at len(src), and the chunks
    have no gaps between consecutive segments."""
    chunks = _chunk(name, src)
    assert chunks
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(src)
    # No gap: each chunk's start must be ≤ previous chunk's end (overlap
    # allowed for paragraph; equality required for AST).
    for prev, nxt in zip(chunks[:-1], chunks[1:], strict=True):
        assert nxt.char_start <= prev.char_end


@pytest.mark.parametrize(
    ("name", "src"),
    [pytest.param(n, s, id=p.id) for p, (n, s) in zip(CASES, [(c.values[0], c.values[1]) for c in CASES], strict=True) if n.endswith((".py", ".ts", ".go"))],
)
def test_code_chunks_do_not_overlap(name: str, src: str) -> None:
    """AST chunks are syntactic — adjacent chunks abut, never overlap."""
    chunks = _chunk(name, src)
    for prev, nxt in zip(chunks[:-1], chunks[1:], strict=True):
        assert nxt.char_start == prev.char_end


@pytest.mark.parametrize(("name", "src"), CASES)
def test_chunks_non_empty(name: str, src: str) -> None:
    for c in _chunk(name, src):
        assert c.char_end > c.char_start
        assert c.text


def test_empty_text_yields_no_chunks_for_any_strategy() -> None:
    for name in ("a.py", "a.ts", "a.go", "a.md", "a.txt"):
        assert _chunk(name, "") == []
