"""Windows ``.url`` (Internet Shortcut) parser.

`.url` files are tiny INI files Windows produces when you drag a link
to a folder:

    [InternetShortcut]
    URL=https://teams.microsoft.com/l/meetup-join/19%3ameeting_...

SharePoint syncs them when users save a Teams meeting link or a web
bookmark into a document library. They carry **no document content**,
but the URL itself is searchable — "where did we link the discovery
call?" is a real question. We render them as a one-line markdown
pointer so the chunker sees the filename + URL together.

We deliberately don't follow Teams links to fetch transcripts here —
that's the dedicated Teams connector's job, separately. This parser
just keeps the file out of the ``unsupported`` bucket.
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import ClassVar

from .base import BaseParser, ParserResult

logger = logging.getLogger(__name__)


class UrlShortcutParser(BaseParser):
    extensions: ClassVar[list[str]] = [".url"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            raw = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParserResult.failure(f"read failed: {e}")

        parser = configparser.RawConfigParser(strict=False)
        try:
            parser.read_string(raw)
        except configparser.Error as e:
            return ParserResult.failure(f"not a valid .url file: {e}")

        url = ""
        try:
            url = parser.get("InternetShortcut", "URL")
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Some apps write the field with different casing; fall back
            # to a case-insensitive walk before giving up.
            for section in parser.sections():
                for key, value in parser.items(section):
                    if key.lower() == "url" and value.strip():
                        url = value.strip()
                        break
                if url:
                    break

        if not url:
            return ParserResult.failure("no URL field found")

        title = file_path.stem
        # One short markdown block — title as H1 + a labeled link.
        # Plain enough that the chunker treats the whole thing as a
        # single chunk and the model sees "<title> → <url>" together.
        content = f"# {title}\n\n[{title}]({url})\n"
        return ParserResult(content=content)
