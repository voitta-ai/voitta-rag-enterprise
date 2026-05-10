"""``AstCodeStrategy`` — language behavior + cAST partition/merge logic."""

from __future__ import annotations

from pathlib import Path

from voitta_rag_enterprise.services.chunking import AstCodeStrategy, chunk_code


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_empty_file() -> None:
    assert chunk_code("", "python") == []


def test_python_single_small_function_is_one_chunk() -> None:
    src = "def foo():\n    return 1\n"
    chunks = chunk_code(src, "python", target_chars=2000)
    assert len(chunks) == 1
    assert chunks[0].text == src
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(src)


def test_python_three_small_functions_merge_to_one_chunk_under_budget() -> None:
    src = (
        "def a():\n    return 1\n\n"
        "def b():\n    return 2\n\n"
        "def c():\n    return 3\n"
    )
    chunks = chunk_code(src, "python", target_chars=2000)
    assert len(chunks) == 1
    assert chunks[0].text == src


def test_python_three_functions_split_when_each_exceeds_budget() -> None:
    body = "    x = 1\n" * 200  # ~2000 chars body
    src = (
        f"def a():\n{body}\n"
        f"def b():\n{body}\n"
        f"def c():\n{body}\n"
    )
    chunks = chunk_code(src, "python", target_chars=2500)
    # Each function body alone is ~2000+ chars → can't pack two together.
    assert len(chunks) >= 3
    # Each chunk slices back to source.
    for c in chunks:
        assert src[c.char_start : c.char_end] == c.text


def test_python_class_with_methods_is_split_when_class_exceeds_budget() -> None:
    body = "        x = 1\n" * 60  # ~1000 chars per method
    src = (
        "class Big:\n"
        f"    def a(self):\n{body}\n"
        f"    def b(self):\n{body}\n"
        f"    def c(self):\n{body}\n"
    )
    chunks = chunk_code(src, "python", target_chars=1500)
    # Class body too big to fit as one chunk → split into method-sized chunks.
    assert len(chunks) >= 2
    for c in chunks:
        assert src[c.char_start : c.char_end] == c.text


def test_python_imports_merge_with_first_function() -> None:
    src = (
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "def main():\n"
        "    return Path(os.getcwd())\n"
    )
    chunks = chunk_code(src, "python", target_chars=2000)
    # Plenty of room — imports + main() should pack into a single chunk.
    assert len(chunks) == 1
    assert "import os" in chunks[0].text
    assert "def main" in chunks[0].text


def test_python_giant_single_string_falls_back_to_char_split() -> None:
    big = "x" * 6000
    src = f's = "{big}"\n'
    chunks = chunk_code(src, "python", target_chars=2000)
    # Can't fit in one chunk; should split into multiple, each under budget.
    assert len(chunks) >= 3
    for c in chunks:
        assert c.char_end - c.char_start <= 2000
        assert src[c.char_start : c.char_end] == c.text


def test_python_malformed_still_chunks() -> None:
    """tree-sitter is permissive — unbalanced syntax shouldn't crash us.

    cAST has no fallback: if the parse tree is broken, we still partition
    whatever ranges tree-sitter produced and the slice invariants hold.
    """
    src = "def broken(  :\n    return\n" + "x" * 100
    chunks = chunk_code(src, "python", target_chars=2000)
    assert len(chunks) >= 1
    for c in chunks:
        assert src[c.char_start : c.char_end] == c.text


def test_python_unicode_offsets_are_char_based() -> None:
    src = '# 你好 world\n\ndef greet():\n    return "héllo"\n'
    chunks = chunk_code(src, "python", target_chars=2000)
    for c in chunks:
        assert src[c.char_start : c.char_end] == c.text


# ---------------------------------------------------------------------------
# Multi-language smoke tests
# ---------------------------------------------------------------------------


def _smoke(text: str, lang: str) -> None:
    chunks = chunk_code(text, lang, target_chars=2000)
    assert chunks
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text
        assert c.char_end - c.char_start <= 2000 or len(text) <= 2000
    # Coverage: union spans the file.
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(text)


def test_typescript() -> None:
    src = (
        "import { foo } from 'bar';\n\n"
        "export function add(a: number, b: number): number {\n"
        "    return a + b;\n"
        "}\n\n"
        "export class Box<T> {\n"
        "    constructor(public v: T) {}\n"
        "}\n"
    )
    _smoke(src, "typescript")


def test_tsx() -> None:
    src = (
        "import React from 'react';\n\n"
        "export function App(): JSX.Element {\n"
        "    return <div>hi</div>;\n"
        "}\n"
    )
    _smoke(src, "tsx")


def test_go() -> None:
    src = (
        "package main\n\n"
        "import \"fmt\"\n\n"
        "func main() {\n"
        "    fmt.Println(\"hi\")\n"
        "}\n"
    )
    _smoke(src, "go")


def test_rust() -> None:
    src = (
        "use std::io;\n\n"
        "fn main() {\n"
        "    println!(\"hi\");\n"
        "}\n"
    )
    _smoke(src, "rust")


def test_java() -> None:
    src = (
        "public class Hello {\n"
        "    public static void main(String[] args) {\n"
        "        System.out.println(\"hi\");\n"
        "    }\n"
        "}\n"
    )
    _smoke(src, "java")


def test_cpp() -> None:
    src = (
        "#include <iostream>\n\n"
        "int main() {\n"
        "    std::cout << \"hi\" << std::endl;\n"
        "    return 0;\n"
        "}\n"
    )
    _smoke(src, "cpp")


def test_ruby() -> None:
    src = "def greet(name)\n  \"hello, #{name}\"\nend\n"
    _smoke(src, "ruby")


# ---------------------------------------------------------------------------
# AstCodeStrategy via Path
# ---------------------------------------------------------------------------


def test_strategy_can_chunk_known_extensions() -> None:
    s = AstCodeStrategy()
    assert s.can_chunk(Path("a.py"))
    assert s.can_chunk(Path("a.ts"))
    assert s.can_chunk(Path("a.go"))
    assert not s.can_chunk(Path("a.md"))
    assert not s.can_chunk(Path("a.unknown"))


def test_strategy_dispatches_to_correct_grammar() -> None:
    s = AstCodeStrategy()
    py = s.chunk("def foo(): pass\n", Path("a.py"))
    rs = s.chunk("fn foo() {}\n", Path("a.rs"))
    assert py and rs
    assert py[0].text == "def foo(): pass\n"
    assert rs[0].text == "fn foo() {}\n"
