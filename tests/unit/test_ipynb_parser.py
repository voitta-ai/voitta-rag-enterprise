"""Tests for ``IpynbParser`` — strips outputs, keeps source."""

from __future__ import annotations

import json
from pathlib import Path

from voitta_rag_enterprise.services.parsers.ipynb_parser import IpynbParser
from voitta_rag_enterprise.services.parsers.registry import build_default_registry


def _write_nb(path: Path, cells: list[dict], language: str = "python") -> Path:
    nb = {
        "cells": cells,
        "metadata": {"kernelspec": {"language": language, "name": language}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.write_text(json.dumps(nb), encoding="utf-8")
    return path


def test_can_parse_only_ipynb() -> None:
    p = IpynbParser()
    assert p.can_parse(Path("notebook.ipynb"))
    assert not p.can_parse(Path("notebook.py"))
    assert not p.can_parse(Path("notebook.json"))


def test_registry_routes_ipynb_to_ipynb_parser(tmp_path: Path) -> None:
    """The dispatcher claims ``.ipynb`` for IpynbParser, not TextParser."""
    r = build_default_registry()
    parser = r.find(tmp_path / "x.ipynb")
    assert isinstance(parser, IpynbParser)


def test_markdown_only_notebook(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "doc.ipynb",
        [
            {"cell_type": "markdown", "source": ["# Title\n", "\n", "First paragraph.\n"]},
            {"cell_type": "markdown", "source": "## Section\n\nMore.\n"},
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "# Title" in r.content
    assert "First paragraph." in r.content
    assert "## Section" in r.content
    # No fenced blocks for a markdown-only notebook.
    assert "```" not in r.content


def test_code_cell_is_fenced_in_kernel_language(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "calc.ipynb",
        [{"cell_type": "code", "source": "x = 1 + 2\nprint(x)\n", "outputs": []}],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "```python" in r.content
    assert "x = 1 + 2" in r.content
    assert "print(x)" in r.content
    assert r.content.rstrip().endswith("```")


def test_kernel_language_other_than_python(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "r.ipynb",
        [{"cell_type": "code", "source": "summary(df)\n", "outputs": []}],
        language="R",
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "```R" in r.content


def test_image_outputs_are_stripped(tmp_path: Path) -> None:
    huge_b64 = "A" * 50_000  # stand-in for a 50KB base64 image payload
    p = _write_nb(
        tmp_path / "fig.ipynb",
        [
            {
                "cell_type": "code",
                "source": "plot()\n",
                "outputs": [
                    {
                        "output_type": "display_data",
                        "data": {
                            "image/png": huge_b64,
                            "image/svg+xml": huge_b64,
                            "text/plain": ["<Figure size 432x288>"],
                        },
                    }
                ],
            }
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    # Source kept.
    assert "plot()" in r.content
    # Image bytes nowhere to be found.
    assert "A" * 100 not in r.content
    # text/plain pulled in alongside the source.
    assert "<Figure size 432x288>" in r.content


def test_stream_output_is_kept_in_text_block(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "stream.ipynb",
        [
            {
                "cell_type": "code",
                "source": "print('hello')\n",
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": ["hello\n"]}
                ],
            }
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "print('hello')" in r.content
    assert "```text" in r.content
    assert "hello" in r.content


def test_stream_output_is_capped(tmp_path: Path) -> None:
    """A stuck-in-a-loop notebook can produce MB of stdout — we cap it."""
    p = _write_nb(
        tmp_path / "loop.ipynb",
        [
            {
                "cell_type": "code",
                "source": "for i in range(10000): print(i)\n",
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": ["x" * 5000],
                    }
                ],
            }
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "[truncated]" in r.content
    assert r.content.count("x") < 5000


def test_error_traceback_is_kept(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "broken.ipynb",
        [
            {
                "cell_type": "code",
                "source": "1/0\n",
                "outputs": [
                    {
                        "output_type": "error",
                        "ename": "ZeroDivisionError",
                        "evalue": "division by zero",
                        "traceback": [
                            "Traceback (most recent call last):",
                            "  File \"<stdin>\", line 1, in <module>",
                            "ZeroDivisionError: division by zero",
                        ],
                    }
                ],
            }
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "ZeroDivisionError" in r.content


def test_empty_cell_is_skipped(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "skip.ipynb",
        [
            {"cell_type": "code", "source": "", "outputs": []},
            {"cell_type": "markdown", "source": "kept"},
            {"cell_type": "code", "source": [], "outputs": []},
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    # Only the markdown cell survived; no stray fenced code blocks.
    assert r.content.strip() == "kept"


def test_raw_cell_is_kept_unfenced(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "raw.ipynb",
        [{"cell_type": "raw", "source": "raw passthrough"}],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert r.content == "raw passthrough"


def test_malformed_json_is_failure(tmp_path: Path) -> None:
    p = tmp_path / "bad.ipynb"
    p.write_text("{ not valid json", encoding="utf-8")
    r = IpynbParser().parse(p)
    assert not r.success
    assert "json decode" in (r.error or "")


def test_missing_cells_returns_empty(tmp_path: Path) -> None:
    """Old / malformed notebooks may lack the ``cells`` array."""
    p = tmp_path / "v3.ipynb"
    p.write_text(json.dumps({"worksheets": []}), encoding="utf-8")
    r = IpynbParser().parse(p)
    assert r.success
    assert r.content == ""


def test_source_can_be_string_or_list(tmp_path: Path) -> None:
    """The notebook spec allows ``source`` as either form."""
    p = _write_nb(
        tmp_path / "mix.ipynb",
        [
            {"cell_type": "code", "source": "a = 1\n", "outputs": []},
            {"cell_type": "code", "source": ["b = 2\n", "c = 3\n"], "outputs": []},
        ],
    )
    r = IpynbParser().parse(p)
    assert r.success
    assert "a = 1" in r.content
    assert "b = 2" in r.content
    assert "c = 3" in r.content


def test_cell_order_is_preserved(tmp_path: Path) -> None:
    p = _write_nb(
        tmp_path / "order.ipynb",
        [
            {"cell_type": "markdown", "source": "FIRST"},
            {"cell_type": "code", "source": "SECOND_CODE", "outputs": []},
            {"cell_type": "markdown", "source": "THIRD"},
        ],
    )
    r = IpynbParser().parse(p)
    pos_first = r.content.find("FIRST")
    pos_second = r.content.find("SECOND_CODE")
    pos_third = r.content.find("THIRD")
    assert 0 <= pos_first < pos_second < pos_third
