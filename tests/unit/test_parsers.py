"""Tests for the parser registry and the lightweight parsers.

The PDF / DOCX / PPTX parsers are exercised via the registry — we don't
build heavyweight fixtures here. The indexing tests cover end-to-end flow.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image as PILImage

from voitta_image_rag.services.parsers.image_parser import ImageFileParser
from voitta_image_rag.services.parsers.registry import (
    build_default_registry,
    get_default_registry,
)
from voitta_image_rag.services.parsers.text_parser import TextParser


def _png_bytes(color: tuple[int, int, int] = (255, 0, 0), size: tuple[int, int] = (8, 8)) -> bytes:
    img = PILImage.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_text_parser_reads_utf8(tmp_path: Path) -> None:
    p = tmp_path / "hello.txt"
    p.write_text("hello world")
    r = TextParser().parse(p)
    assert r.success
    assert r.content == "hello world"
    assert r.images == []


def test_text_parser_can_parse_extensions() -> None:
    parser = TextParser()
    assert parser.can_parse(Path("a.md"))
    assert parser.can_parse(Path("a.PY"))  # case-insensitive
    assert not parser.can_parse(Path("a.pdf"))


def test_image_file_parser_returns_one_image(tmp_path: Path) -> None:
    p = tmp_path / "logo.png"
    p.write_bytes(_png_bytes())
    r = ImageFileParser().parse(p)
    assert r.success
    assert r.content == ""
    assert len(r.images) == 1
    img = r.images[0]
    assert img.mime == "image/png"
    assert (img.width, img.height) == (8, 8)
    assert img.position == 0


def test_image_file_parser_rejects_non_image(tmp_path: Path) -> None:
    p = tmp_path / "fake.png"
    p.write_text("not actually a png")
    r = ImageFileParser().parse(p)
    assert not r.success
    assert "decode" in (r.error or "")


def test_registry_dispatches_by_extension(tmp_path: Path) -> None:
    r = build_default_registry()
    assert r.find(Path("x.md")) is not None
    assert r.find(Path("x.png")) is not None
    assert r.find(Path("x.pdf")) is not None
    assert r.find(Path("x.docx")) is not None
    assert r.find(Path("x.pptx")) is not None
    assert r.find(Path("x.xyz")) is None


def test_default_registry_is_cached() -> None:
    assert get_default_registry() is get_default_registry()
