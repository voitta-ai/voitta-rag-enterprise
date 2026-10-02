"""EPUB parser (stdlib zip/xml + BeautifulSoup/lxml — no extra dependency).

An EPUB is a zip of XHTML chapters. We read ``META-INF/container.xml`` to find
the OPF package, take title/author/language from its metadata, and walk the
*spine* (the reading order) converting each chapter to markdown. Images are
kept: each ``<img>`` / SVG ``<image>`` becomes an ``ExtractedImage`` anchored at
the char offset where it appears (same contract as the DOCX parser), so figures
stay next to the text that discusses them.

Image handling:
  * tiny decorations (bullets, rules, spacers) are dropped, and an image file
    referenced many times (chapter ornaments) is emitted once;
  * an oversized raster (long edge > ``MAX_IMG_EDGE``) is downscaled and stored
    as WebP — a storage safeguard only, since the embedder and the retrieval
    layer already downscale; everything else is stored byte-for-byte;
  * SVG figures are rasterised at 512 px via PyMuPDF (as the SVG parser does);
  * a dangling image reference is skipped, never fatal.

DRM-protected books raise ``UnsupportedDocumentError`` (parked as
``unsupported`` with a clear reason, like a password-protected PDF).
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import warnings
import zipfile
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from bs4 import XMLParsedAsHTMLWarning

from ._ooxml import MIN_IMG_DIM, image_dimensions
from .base import (
    BaseParser,
    ExtractedImage,
    ParserResult,
    UnsupportedDocumentError,
)

logger = logging.getLogger(__name__)

# Long-edge cap for stored rasters. Charts stay legible at this size; only
# pathological scans (thousands of px) get shrunk.
MAX_IMG_EDGE = 2000
_WEBP_QUALITY = 90
# Files smaller than this that also fail to decode are treated as decoration.
_MIN_IMG_BYTES = 512
_SVG_RASTER_WIDTH = 512
# Guard against a zip bomb / absurd chapter: skip (with a log) anything bigger.
_MAX_CHAPTER_BYTES = 30 * 1024 * 1024
_MAX_IMAGE_BYTES = 40 * 1024 * 1024

_RASTER_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}

# Encryption algorithms that only obfuscate embedded *fonts* — the text and
# images stay readable, so these are not DRM.
_FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

_NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_NS_OPF = "{http://www.idpf.org/2007/opf}"
_NS_DC = "{http://purl.org/dc/elements/1.1/}"
_NS_ENC = "{http://www.w3.org/2001/04/xmlenc#}"

_SKIP_TAGS = {"head", "script", "style", "nav", "title", "meta", "link", "noscript"}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_CONTAINER_TAGS = {
    "html", "body", "div", "section", "article", "aside", "main", "header",
    "footer", "figure", "figcaption", "blockquote", "details", "summary",
    "dl", "dd", "dt", "center", "address", "tbody", "thead", "tfoot",
}
_PARA_TAGS = {"p"}
_BLOCK_TAGS = (
    _CONTAINER_TAGS | _PARA_TAGS | set(_HEADINGS)
    | {"ul", "ol", "table", "pre", "hr"}
)
_BR = "\x00BR\x00"
# Alt text that is really a file name ("cover.jpg", "/title-page.jpg").
_FILENAME_ALT = re.compile(r"[/\\]|\.(?:jpe?g|png|gif|webp|svg|bmp|tiff?)$", re.I)
_WS = re.compile(r"[ \t\r\n\f\v ]+")


class _Emitter:
    """Collects markdown blocks and image references for the whole book."""

    def __init__(self) -> None:
        self.blocks: list[str] = []
        # (index into ``blocks`` of the next block to be emitted, zip path)
        self.images: list[tuple[int, str]] = []
        self.chapter_dir = ""

    def text(self, s: str) -> None:
        s = s.strip()
        if s:
            self.blocks.append(s)

    def image(self, href: str | None) -> None:
        if not href:
            return
        href = urldefrag(unquote(href.strip()))[0]
        if not href or href.startswith(("data:", "http:", "https:")):
            return
        path = posixpath.normpath(posixpath.join(self.chapter_dir, href))
        self.images.append((len(self.blocks), path))


# ---------------------------------------------------------------------------
# HTML → markdown
# ---------------------------------------------------------------------------


def _collapse(s: str) -> str:
    return _WS.sub(" ", s)


def _inline(nodes, em: _Emitter, found: list[str | None]) -> str:
    """Render inline nodes to text; image hrefs are appended to ``found``."""
    out: list[str] = []
    for n in nodes:
        if isinstance(n, Comment):
            continue
        if isinstance(n, NavigableString):
            out.append(_collapse(str(n)))
            continue
        if not isinstance(n, Tag):
            continue
        name = (n.name or "").lower()
        if name in _SKIP_TAGS:
            continue
        if name == "br":
            out.append(_BR)
        elif name == "img":
            found.append(n.get("src"))
            alt = (n.get("alt") or "").strip()
            if alt and len(alt) > 2 and not _FILENAME_ALT.search(alt):
                out.append(f" {alt} ")
        elif name == "image":  # SVG <image xlink:href=…>
            found.append(n.get("xlink:href") or n.get("href"))
        elif name in ("strong", "b", "em", "i", "code", "tt"):
            inner = _inline(n.children, em, found)
            core = inner.strip()
            if core and _BR not in core:
                mark = {"strong": "**", "b": "**", "em": "*", "i": "*"}.get(name, "`")
                lead = inner[: len(inner) - len(inner.lstrip())]
                trail = inner[len(inner.rstrip()):]
                out.append(f"{lead}{mark}{core}{mark}{trail}")
            else:
                out.append(inner)
        else:
            out.append(_inline(n.children, em, found))
    return "".join(out)


def _finish_inline(s: str) -> str:
    s = _collapse(s).replace(_BR, "\n")
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


def _emit_inline(nodes, em: _Emitter, prefix: str = "") -> None:
    found: list[str | None] = []
    text = _finish_inline(_inline(nodes, em, found))
    if text:
        em.text(prefix + text)
    for href in found:
        em.image(href)


def _walk_container(el: Tag, em: _Emitter) -> None:
    buf: list = []

    def flush() -> None:
        if buf:
            _emit_inline(buf, em)
            buf.clear()

    for ch in el.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            buf.append(ch)
        elif isinstance(ch, Tag):
            name = (ch.name or "").lower()
            if name in _SKIP_TAGS:
                continue
            if name in _BLOCK_TAGS:
                flush()
                _block(ch, em)
            else:
                buf.append(ch)
    flush()


def _list_lines(el: Tag, em: _Emitter, depth: int = 0) -> list[str]:
    """Render a list as lines (kept as ONE block so items stay adjacent and
    nesting indentation survives the block-level strip)."""
    ordered = (el.name or "").lower() == "ol"
    lines: list[str] = []
    n = 0
    for li in el.children:
        if not isinstance(li, Tag) or (li.name or "").lower() != "li":
            continue
        n += 1
        marker = f"{n}." if ordered else "-"
        inline_nodes = []
        nested: list[Tag] = []
        for ch in li.children:
            if isinstance(ch, Tag) and (ch.name or "").lower() in ("ul", "ol"):
                nested.append(ch)
            else:
                inline_nodes.append(ch)
        found: list[str | None] = []
        text = _finish_inline(_inline(inline_nodes, em, found)).replace("\n", " ")
        if text:
            lines.append("  " * depth + f"{marker} {text}")
        for href in found:
            em.image(href)
        for sub in nested:
            lines.extend(_list_lines(sub, em, depth + 1))
    return lines


def _list(el: Tag, em: _Emitter) -> None:
    lines = _list_lines(el, em)
    if lines:
        # Bypass ``em.text``'s strip: it would eat the first line's indent only
        # at depth 0 (none), so a plain join is safe here.
        em.blocks.append("\n".join(lines))


def _table(el: Tag, em: _Emitter) -> None:
    rows: list[list[str]] = []
    for tr in el.find_all("tr"):
        cells = []
        for td in tr.find_all(["td", "th"], recursive=False):
            found: list[str | None] = []
            text = _finish_inline(_inline(td.children, em, found)).replace("\n", " ")
            cells.append(text.replace("|", "\\|"))
            for href in found:
                em.image(href)
        if cells:
            rows.append(cells)
    if not rows:
        return
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    em.text("\n".join(lines))


def _block(el: Tag, em: _Emitter) -> None:
    name = (el.name or "").lower()
    if name in _HEADINGS:
        _emit_inline(el.children, em, prefix="#" * _HEADINGS[name] + " ")
    elif name in _PARA_TAGS:
        if any(isinstance(c, Tag) and (c.name or "").lower() in _BLOCK_TAGS
               for c in el.children):
            _walk_container(el, em)
        else:
            _emit_inline(el.children, em)
    elif name in ("ul", "ol"):
        _list(el, em)
    elif name == "table":
        _table(el, em)
    elif name == "pre":
        em.text("```\n" + el.get_text().strip("\n") + "\n```")
    elif name == "hr":
        return
    else:
        _walk_container(el, em)


# ---------------------------------------------------------------------------
# Package handling
# ---------------------------------------------------------------------------


def _xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def _check_drm(zf: zipfile.ZipFile) -> None:
    try:
        raw = zf.read("META-INF/encryption.xml")
    except KeyError:
        return
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return
    for enc in root.iter(f"{_NS_ENC}EncryptionMethod"):
        if enc.get("Algorithm", "") not in _FONT_OBFUSCATION:
            raise UnsupportedDocumentError("DRM-protected EPUB")


def _rootfile(zf: zipfile.ZipFile) -> str:
    root = _xml(zf, "META-INF/container.xml")
    rf = root.find(f".//{_NS_CONTAINER}rootfile")
    path = rf.get("full-path") if rf is not None else None
    if not path:
        raise ValueError("container.xml has no rootfile")
    return path


def _meta(opf: ET.Element, tag: str) -> str:
    el = opf.find(f".//{_NS_DC}{tag}")
    return (el.text or "").strip() if el is not None and el.text else ""


def _spine_paths(opf: ET.Element, opf_dir: str) -> list[str]:
    manifest: dict[str, tuple[str, str, str]] = {}
    for it in opf.iterfind(f".//{_NS_OPF}manifest/{_NS_OPF}item"):
        manifest[it.get("id", "")] = (
            it.get("href", ""), it.get("media-type", ""), it.get("properties", ""),
        )
    paths: list[str] = []
    for ref in opf.iterfind(f".//{_NS_OPF}spine/{_NS_OPF}itemref"):
        href, mtype, props = manifest.get(ref.get("idref", ""), ("", "", ""))
        if not href or "nav" in props.split():
            continue
        if mtype not in ("application/xhtml+xml", "text/html", "application/xml"):
            continue
        paths.append(posixpath.normpath(posixpath.join(opf_dir, unquote(href))))
    return paths


def _rasterize_svg(blob: bytes) -> tuple[bytes, int, int] | None:
    try:
        import pymupdf

        with pymupdf.open(stream=blob, filetype="svg") as doc:
            page = doc[0]
            if page.rect.width <= 0:
                return None
            zoom = _SVG_RASTER_WIDTH / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            return pix.tobytes("png"), pix.width, pix.height
    except Exception as e:  # noqa: BLE001 - best effort, never fatal
        logger.debug("epub svg rasterize failed: %s", e)
        return None


def _cap_size(blob: bytes, mime: str, width: int, height: int) -> tuple[bytes, str, int, int]:
    """Downscale an oversized raster to WebP; otherwise return it untouched."""
    if max(width, height) <= MAX_IMG_EDGE or mime == "image/gif":
        return blob, mime, width, height
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(blob)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            scale = MAX_IMG_EDGE / max(img.size)
            size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.resize(size, PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=_WEBP_QUALITY)
            return buf.getvalue(), "image/webp", size[0], size[1]
    except Exception as e:  # noqa: BLE001 - keep the original on any failure
        logger.debug("epub image downscale failed: %s", e)
        return blob, mime, width, height


def _load_image(zf: zipfile.ZipFile, path: str) -> ExtractedImage | None:
    """Read + vet one image; ``None`` means skip it."""
    try:
        info = zf.getinfo(path)
    except KeyError:
        logger.debug("epub: dangling image reference %s", path)
        return None
    if info.file_size > _MAX_IMAGE_BYTES:
        logger.info("epub: skipping oversized image %s (%d bytes)", path, info.file_size)
        return None
    ext = posixpath.splitext(path)[1].lower()
    try:
        blob = zf.read(path)
    except (zipfile.BadZipFile, OSError, RuntimeError) as e:
        logger.debug("epub: cannot read image %s: %s", path, e)
        return None

    if ext == ".svg":
        rast = _rasterize_svg(blob)
        if rast is None:
            return None
        png, w, h = rast
        if max(w, h) < MIN_IMG_DIM:
            return None
        return ExtractedImage(bytes=png, mime="image/png", position=0, width=w, height=h)

    mime = _RASTER_MIME.get(ext)
    if mime is None:
        return None
    width, height = image_dimensions(blob)
    if width and height:
        if max(width, height) < MIN_IMG_DIM:
            return None
        blob, mime, width, height = _cap_size(blob, mime, width, height)
    elif len(blob) < _MIN_IMG_BYTES:
        return None
    return ExtractedImage(bytes=blob, mime=mime, position=0, width=width, height=height)


class EpubParser(BaseParser):
    extensions: ClassVar[list[str]] = [".epub"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            zf = zipfile.ZipFile(file_path)
        except (zipfile.BadZipFile, OSError) as e:
            return ParserResult.failure(f"epub open failed: {e}")
        with zf:
            _check_drm(zf)
            try:
                opf_path = _rootfile(zf)
                opf = _xml(zf, opf_path)
            except (KeyError, ET.ParseError, ValueError) as e:
                return ParserResult.failure(f"epub package unreadable: {e}")
            opf_dir = posixpath.dirname(opf_path)
            chapters = _spine_paths(opf, opf_dir)
            if not chapters:
                return ParserResult.failure("epub has an empty reading order")

            title = _meta(opf, "title")
            author = _meta(opf, "creator")
            language = _meta(opf, "language")

            em = _Emitter()
            if title:
                em.text(f"# {title}")
            if author:
                em.text(f"by {author}")
            header_blocks = len(em.blocks)

            read = 0
            for ch_path in chapters:
                try:
                    info = zf.getinfo(ch_path)
                    if info.file_size > _MAX_CHAPTER_BYTES:
                        logger.info("epub: skipping huge chapter %s", ch_path)
                        continue
                    data = zf.read(ch_path)
                except (KeyError, zipfile.BadZipFile, OSError) as e:
                    logger.debug("epub: cannot read chapter %s: %s", ch_path, e)
                    continue
                read += 1
                em.chapter_dir = posixpath.dirname(ch_path)
                with warnings.catch_warnings():
                    # EPUB chapters are XHTML; the tolerant HTML parser is what
                    # we want, and the per-chapter warning would flood the logs.
                    warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
                    soup = BeautifulSoup(data, "lxml")
                root = soup.body or soup
                _walk_container(root, em)

            # Vet images once per file; an ornament reused 50 times is emitted once.
            cache: dict[str, ExtractedImage | None] = {}
            placed: list[tuple[int, ExtractedImage]] = []
            seen: set[str] = set()
            for block_idx, path in em.images:
                if path in seen:
                    continue
                seen.add(path)
                if path not in cache:
                    cache[path] = _load_image(zf, path)
                img = cache[path]
                if img is not None:
                    placed.append((block_idx, img))

        if len(em.blocks) == header_blocks and not placed:
            return ParserResult.failure("epub has no readable content")

        offsets: list[int] = []
        cursor = 0
        for b in em.blocks:
            offsets.append(cursor)
            cursor += len(b) + 2
        content = "\n\n".join(em.blocks)
        images: list[ExtractedImage] = []
        for block_idx, img in placed:
            img.position = min(
                offsets[block_idx] if block_idx < len(offsets) else len(content),
                len(content),
            )
            images.append(img)

        return ParserResult(
            content=content,
            images=images,
            metadata={
                "source_format": "epub",
                "title": title,
                "author": author,
                "language": language,
                "chapters": read,
            },
        )
