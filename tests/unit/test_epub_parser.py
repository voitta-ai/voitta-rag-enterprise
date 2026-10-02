"""EPUB parser: structure, markup, images, DRM, and failure modes."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from voitta_rag_enterprise.services.parsers.base import UnsupportedDocumentError
from voitta_rag_enterprise.services.parsers.epub_parser import MAX_IMG_EDGE, EpubParser
from voitta_rag_enterprise.services.parsers.registry import build_default_registry

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"
 media-type="application/oebps-package+xml"/></rootfiles></container>"""


def opf(spine_ids, items, title="Test Book", author="Ann Author"):
    manifest = "".join(
        f'<item id="{i}" href="{h}" media-type="{m}"{(" properties=%r" % p).replace(chr(39), chr(34)) if p else ""}/>'
        for i, h, m, p in items
    )
    spine = "".join(f'<itemref idref="{i}"/>' for i in spine_ids)
    return f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
<dc:language>en</dc:language></metadata>
<manifest>{manifest}</manifest><spine>{spine}</spine></package>"""


def xhtml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
        f"<body>{body}</body></html>"
    )


def png(size=(100, 80), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def build(tmp_path: Path, chapters: dict[str, str], files=None, *, spine=None,
          extra_items=(), encryption: str | None = None, **meta) -> Path:
    items = [(f"c{i}", name, "application/xhtml+xml", "") for i, name in enumerate(chapters)]
    items += list(extra_items)
    spine_ids = spine if spine is not None else [f"c{i}" for i in range(len(chapters))]
    p = tmp_path / "book.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        if encryption:
            z.writestr("META-INF/encryption.xml", encryption)
        z.writestr("OEBPS/content.opf", opf(spine_ids, items, **meta))
        for name, body in chapters.items():
            z.writestr(f"OEBPS/{name}", body)
        for name, blob in (files or {}).items():
            z.writestr(f"OEBPS/{name}", blob)
    return p


def enc(alg: str) -> str:
    return (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        f'<enc:EncryptionMethod Algorithm="{alg}"/><enc:CipherData>'
        '<enc:CipherReference URI="OEBPS/x"/></enc:CipherData></enc:EncryptedData></encryption>'
    )


def test_spine_order_and_metadata(tmp_path):
    p = build(
        tmp_path,
        {"a.xhtml": xhtml("<p>First chapter</p>"), "b.xhtml": xhtml("<p>Second chapter</p>")},
        spine=["c1", "c0"],
    )
    r = EpubParser().parse(p)
    assert r.success
    assert r.content.index("Second chapter") < r.content.index("First chapter")
    assert r.content.startswith("# Test Book\n\nby Ann Author")
    assert r.metadata["title"] == "Test Book"
    assert r.metadata["author"] == "Ann Author"
    assert r.metadata["language"] == "en"
    assert r.metadata["chapters"] == 2


def test_nav_document_and_non_spine_files_ignored(tmp_path):
    p = build(
        tmp_path,
        {"a.xhtml": xhtml("<p>Real text</p>"), "nav.xhtml": xhtml("<p>TOC junk</p>"),
         "orphan.xhtml": xhtml("<p>Orphan</p>")},
        spine=["c0", "c1"],
        extra_items=[],
    )
    # mark c1 as the nav document
    with zipfile.ZipFile(p) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files["OEBPS/content.opf"] = files["OEBPS/content.opf"].replace(
        b'href="nav.xhtml" media-type="application/xhtml+xml"',
        b'href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"',
    )
    with zipfile.ZipFile(p, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    r = EpubParser().parse(p)
    assert "Real text" in r.content
    assert "TOC junk" not in r.content
    assert "Orphan" not in r.content


def test_markup_to_markdown(tmp_path):
    body = (
        "<h2>Chapter One</h2><p>Some <strong>bold</strong> and <em>italic</em> text.</p>"
        "<ul><li>alpha</li><li>beta<ul><li>nested</li></ul></li></ul>"
        "<ol><li>one</li><li>two</li></ol>"
        "<table><tr><th>Year</th><th>Rate</th></tr><tr><td>2020</td><td>1.5%</td></tr></table>"
        "<script>evil()</script><style>.x{}</style>"
    )
    r = EpubParser().parse(build(tmp_path, {"a.xhtml": xhtml(body)}))
    c = r.content
    assert "## Chapter One" in c
    assert "Some **bold** and *italic* text." in c
    assert "- alpha" in c and "  - nested" in c
    assert "1. one" in c and "2. two" in c
    assert "| Year | Rate |" in c and "| 2020 | 1.5% |" in c
    assert "evil" not in c and ".x{}" not in c


def test_image_placed_at_its_text_offset(tmp_path):
    body = '<p>Before the figure.</p><p><img src="images/fig.png" alt="chart"/></p><p>After the figure.</p>'
    p = build(tmp_path, {"a.xhtml": xhtml(body)}, files={"images/fig.png": png((400, 300))})
    r = EpubParser().parse(p)
    assert len(r.images) == 1
    img = r.images[0]
    assert (img.width, img.height) == (400, 300)
    assert img.mime == "image/png"
    # anchored between the two paragraphs: after "Before", at/before "After"
    assert r.content.index("Before the figure.") < img.position <= r.content.index("After the figure.")


def test_relative_image_paths_and_subdir_chapters(tmp_path):
    p = build(
        tmp_path,
        {"text/ch1.xhtml": xhtml('<p>x</p><img src="../images/f.png"/>')},
        files={"images/f.png": png((200, 200))},
    )
    assert len(EpubParser().parse(p).images) == 1


def test_tiny_dangling_and_repeated_images(tmp_path):
    body = (
        '<p>t</p><img src="i/dot.png"/><img src="i/missing.png"/>'
        '<img src="i/orn.png"/><p>u</p><img src="i/orn.png"/><img src="i/orn.png"/>'
    )
    p = build(
        tmp_path,
        {"a.xhtml": xhtml(body)},
        files={"i/dot.png": png((8, 8)), "i/orn.png": png((120, 40))},
    )
    r = EpubParser().parse(p)
    assert r.success
    assert len(r.images) == 1  # dot dropped, missing skipped, ornament emitted once


def test_normal_image_stored_untouched_and_oversize_capped(tmp_path):
    normal = png((1500, 900))
    huge = png((MAX_IMG_EDGE * 2, MAX_IMG_EDGE))
    body = '<p>a</p><img src="n.png"/><p>b</p><img src="h.png"/>'
    p = build(tmp_path, {"a.xhtml": xhtml(body)}, files={"n.png": normal, "h.png": huge})
    imgs = EpubParser().parse(p).images
    assert len(imgs) == 2
    n, h = imgs
    assert n.bytes == normal and n.mime == "image/png"
    assert h.mime == "image/webp"
    assert max(h.width, h.height) == MAX_IMG_EDGE
    with Image.open(io.BytesIO(h.bytes)) as im:
        assert max(im.size) == MAX_IMG_EDGE


def test_svg_image_rasterized(tmp_path):
    pytest.importorskip("pymupdf")
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
        b'<rect width="300" height="200" fill="blue"/></svg>'
    )
    body = '<p>t</p><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="f.svg"/></svg>'
    p = build(tmp_path, {"a.xhtml": xhtml(body)}, files={"f.svg": svg})
    imgs = EpubParser().parse(p).images
    assert len(imgs) == 1
    assert imgs[0].mime == "image/png" and imgs[0].width == 512


def test_drm_rejected_but_font_obfuscation_ok(tmp_path):
    chapters = {"a.xhtml": xhtml("<p>hello</p>")}
    with pytest.raises(UnsupportedDocumentError, match="DRM"):
        EpubParser().parse(build(tmp_path, chapters, encryption=enc("http://www.w3.org/2001/04/xmlenc#aes128-cbc")))
    ok = EpubParser().parse(build(tmp_path, chapters, encryption=enc("http://www.idpf.org/2008/embedding")))
    assert ok.success and "hello" in ok.content


def test_failure_modes(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip at all")
    r = EpubParser().parse(bad)
    assert not r.success and "open failed" in r.error

    no_container = tmp_path / "nc.epub"
    with zipfile.ZipFile(no_container, "w") as z:
        z.writestr("hello.txt", "x")
    r = EpubParser().parse(no_container)
    assert not r.success and "unreadable" in r.error

    # A title/author header with an empty body is not indexable content.
    r = EpubParser().parse(build(tmp_path, {"a.xhtml": xhtml("")}))
    assert not r.success and "no readable content" in r.error


def test_empty_book_with_no_metadata_fails(tmp_path):
    p = build(tmp_path, {"a.xhtml": xhtml("<p>   </p>")}, title="", author="")
    r = EpubParser().parse(p)
    assert not r.success and "no readable content" in r.error


def test_registry_routes_epub():
    parser = build_default_registry().find(Path("Some Book.EPUB"))
    assert isinstance(parser, EpubParser)


def test_filename_alt_text_not_leaked_and_no_xml_warning(tmp_path, recwarn):
    body = (
        '<p>t</p><img src="c.png" alt="cover-front.jpg"/>'
        '<img src="d.png" alt="/title-page.jpg"/><img src="e.png" alt="Revenue by year"/>'
    )
    p = build(tmp_path, {"a.xhtml": xhtml(body)},
              files={"c.png": png(), "d.png": png(), "e.png": png()})
    r = EpubParser().parse(p)
    assert "cover-front.jpg" not in r.content and "title-page" not in r.content
    assert "Revenue by year" in r.content
    assert not [w for w in recwarn if "XMLParsedAsHTML" in str(w.category)]
