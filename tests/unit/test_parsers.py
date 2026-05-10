"""Tests for the parser registry and the lightweight parsers.

The PDF / DOCX / PPTX parsers are exercised via the registry — we don't
build heavyweight fixtures here. The indexing tests cover end-to-end flow.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image as PILImage

from voitta_rag_enterprise.services.parsers.image_parser import ImageFileParser
from voitta_rag_enterprise.services.parsers.registry import (
    build_default_registry,
    get_default_registry,
)
from voitta_rag_enterprise.services.parsers.text_parser import TextParser


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


def test_text_parser_covers_common_code_extensions() -> None:
    """Spot-check the expanded code-file allowlist.

    The list is open-ended — every entry here was a real "why isn't my X
    file indexed?" report waiting to happen. Add to this test whenever the
    extension list grows so we don't regress a recovered case.
    """
    parser = TextParser()
    must_match = [
        "x.php", "x.rb", "x.swift", "x.kt", "x.scala", "x.lua", "x.r",
        "x.dart", "x.ex", "x.hs", "x.cs", "x.vue", "x.svelte", "x.scss",
        "x.bash", "x.zsh", "x.ini", "x.conf", "x.tex", "x.patch",
        # build / template / infra extensions added for git-project coverage
        "MANIFEST.in", "config.tpl", "values.tmpl", "vars.example",
        "main.tf", "vars.tfvars", "infra.hcl",
        "schema.proto", "service.thrift", "query.graphql",
        "rules.bzl", "BUILD.bazel",
        "msgs.po", "kernel.cu",
    ]
    for name in must_match:
        assert parser.can_parse(Path(name)), f"{name} should be parseable"


def test_text_parser_matches_extensionless_project_files() -> None:
    """Files like LICENSE / Dockerfile / Makefile have no usable extension.

    Real git projects are full of them; before adding ``filenames`` matching
    they were silently skipped because ``Path('LICENSE').suffix`` is ``''``.
    """
    parser = TextParser()
    must_match = [
        "LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CHANGELOG", "README",
        "Makefile", "GNUmakefile", "Dockerfile", "Containerfile",
        "Procfile", "Vagrantfile", "Jenkinsfile", "Gemfile", "Rakefile",
        "Pipfile", "BUILD", "BUILD.bazel", "WORKSPACE",
        "Cargo.lock", "Pipfile.lock", "poetry.lock", "uv.lock", "yarn.lock",
        ".gitignore", ".gitattributes", ".dockerignore",
        ".editorconfig", ".npmrc", ".python-version", ".env",
        ".flake8", ".eslintrc", ".prettierrc",
    ]
    for name in must_match:
        assert parser.can_parse(Path(name)), f"{name} should be parseable"


def test_text_parser_still_rejects_binary_extensions() -> None:
    """The permissive name list mustn't pull in binary blobs."""
    parser = TextParser()
    must_not_match = ["x.png", "x.pdf", "x.docx", "x.xlsx", "x.mp4", "x.zip"]
    for name in must_not_match:
        assert not parser.can_parse(Path(name)), f"{name} should NOT be parseable"


def test_text_parser_matches_extensionless_file_in_subdir(tmp_path: Path) -> None:
    """``filenames`` lookup works regardless of the parent path."""
    parser = TextParser()
    p = tmp_path / "subproject" / "LICENSE"
    p.parent.mkdir(parents=True)
    p.write_text("MIT License\n\nPermission is hereby granted, ...")
    assert parser.can_parse(p)
    r = parser.parse(p)
    assert r.success
    assert "MIT License" in r.content


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
    assert r.find(Path("x.svg")) is not None
    assert r.find(Path("x.xyz")) is None


def test_default_registry_is_cached() -> None:
    assert get_default_registry() is get_default_registry()


def test_svg_parser_rasterizes_to_png(tmp_path: Path) -> None:
    pytest = __import__("pytest")
    cairosvg = pytest.importorskip("cairosvg")
    del cairosvg  # only used to gate the test
    from voitta_rag_enterprise.services.parsers.svg_parser import SvgParser

    p = tmp_path / "shape.svg"
    p.write_text(
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        '<rect width="20" height="20" fill="red"/></svg>'
    )
    r = SvgParser().parse(p)
    assert r.success
    assert r.content == ""
    assert len(r.images) == 1
    img = r.images[0]
    assert img.mime == "image/png"
    # cairosvg honours output_width=512 with aspect-preserved height; for a
    # square source that's 512x512.
    assert img.width and img.width >= 256
    assert img.height and img.height >= 256
    # Sanity: output really is a PNG.
    assert img.bytes[:8] == b"\x89PNG\r\n\x1a\n"
