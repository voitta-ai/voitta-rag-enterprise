"""Strategy dispatch — by file extension."""

from __future__ import annotations

from pathlib import Path

from voitta_rag_enterprise.services.chunking import (
    AstCodeStrategy,
    ParagraphStrategy,
    build_default_registry,
)


def test_python_routes_to_ast_strategy() -> None:
    r = build_default_registry()
    assert isinstance(r.find(Path("foo.py")), AstCodeStrategy)


def test_typescript_routes_to_ast_strategy() -> None:
    r = build_default_registry()
    assert isinstance(r.find(Path("a/b/foo.ts")), AstCodeStrategy)
    assert isinstance(r.find(Path("foo.tsx")), AstCodeStrategy)


def test_go_rust_java_cpp_route_to_ast_strategy() -> None:
    r = build_default_registry()
    for name in ("main.go", "lib.rs", "Foo.java", "x.cpp", "x.h"):
        assert isinstance(r.find(Path(name)), AstCodeStrategy), name


def test_markdown_routes_to_paragraph() -> None:
    r = build_default_registry()
    assert isinstance(r.find(Path("README.md")), ParagraphStrategy)


def test_text_and_data_route_to_paragraph() -> None:
    r = build_default_registry()
    for name in ("notes.txt", "config.json", "values.yaml", "data.csv", "x.rst"):
        assert isinstance(r.find(Path(name)), ParagraphStrategy), name


def test_unknown_extension_routes_to_paragraph() -> None:
    r = build_default_registry()
    assert isinstance(r.find(Path("blob.unknownext")), ParagraphStrategy)


def test_extension_match_is_case_insensitive() -> None:
    r = build_default_registry()
    assert isinstance(r.find(Path("FOO.PY")), AstCodeStrategy)
    assert isinstance(r.find(Path("Main.Java")), AstCodeStrategy)


def test_paragraph_strategy_is_last() -> None:
    r = build_default_registry()
    assert isinstance(r.strategies[-1], ParagraphStrategy)


def test_registry_chunk_dispatches_correctly() -> None:
    r = build_default_registry()
    py_chunks = r.chunk("def foo():\n    return 1\n", Path("a.py"))
    md_chunks = r.chunk("# Title\n\nbody\n", Path("a.md"))
    assert py_chunks and md_chunks
    # Sanity: chunks slice back to source for both strategies.
    py_src = "def foo():\n    return 1\n"
    md_src = "# Title\n\nbody\n"
    for c in py_chunks:
        assert py_src[c.char_start : c.char_end] == c.text
    for c in md_chunks:
        assert md_src[c.char_start : c.char_end] == c.text
