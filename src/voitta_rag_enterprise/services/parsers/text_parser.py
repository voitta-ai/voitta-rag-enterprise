"""Plain-text / source-code parser. No images.

Matches a file when *either* its extension is in :attr:`extensions` *or*
its full filename is in :attr:`filenames`. The filename path catches the
extensionless conventions every project has — ``LICENSE``, ``Dockerfile``,
``Makefile``, ``MANIFEST.in``, ``.gitignore``, etc. — without having to
shoehorn them into the extension list (``LICENSE`` has no extension at
all; ``MANIFEST.in`` has the very-overloaded ``.in``).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from .base import BaseParser, ParserResult

# Bytes sampled from the head of an extensionless file to decide whether it
# is plain text. Big enough to catch an embedded NUL / bad encoding, small
# enough to stay cheap on large blobs.
_TEXT_SNIFF_BYTES = 8192


def _looks_like_text(file_path: Path, sniff_bytes: int = _TEXT_SNIFF_BYTES) -> bool:
    """Best-effort "is this a UTF-8 text file?" check by sampling the head.

    An embedded NUL byte is the classic binary tell; a sample that then
    decodes as UTF-8 is treated as text. We tolerate a multi-byte sequence
    being sliced at the sample boundary by re-checking with a lenient decode
    and requiring the bulk of the sample to be printable. Empty files are
    not text — there's nothing to index.
    """
    try:
        with file_path.open("rb") as fh:
            chunk = fh.read(sniff_bytes)
    except OSError:
        return False
    if not chunk or b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        text = chunk.decode("utf-8", errors="ignore")
        if not text:
            return False
        printable = sum(ch.isprintable() or ch in "\n\r\t\f\v" for ch in text)
        return printable / len(text) >= 0.90


class TextParser(BaseParser):
    # Anything that's plain UTF-8 text. Files matching either ``extensions``
    # *or* ``filenames`` route through this parser; the dispatcher
    # (registry.py) hands a file to TextParser iff one of them claims it,
    # so missing entries here = silently dropped on indexing.
    extensions: ClassVar[list[str]] = [
        # plain text + docs
        ".txt", ".md", ".markdown", ".rst", ".rst_t", ".log", ".tex", ".bib",
        ".adoc", ".asciidoc", ".org", ".patch", ".diff", ".man",
        # doc-markup variants — Markdown supersets / wiki / lightweight markup.
        # All plain UTF-8 text; embedded JSX/components (.mdx/.mdoc) are just
        # tag noise that's fine for semantic indexing.
        ".mdx", ".mdoc", ".markdoc", ".mkd", ".mkdn", ".mdown", ".litcoffee",
        ".textile", ".pod", ".rdoc", ".wiki", ".mediawiki", ".creole",
        ".asc", ".text",
        # data + config
        ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".csv", ".tsv",
        ".xml", ".ini", ".cfg", ".cnf", ".conf", ".env", ".properties", ".sql",
        # generic build/template extensions seen in real projects
        ".in", ".tpl", ".tmpl", ".template", ".example", ".sample", ".dist",
        # web
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".styl",
        ".vue", ".svelte", ".astro", ".php",
        # python / js / ts
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
        # systems
        ".c", ".cc", ".cxx", ".cpp", ".h", ".hh", ".hxx", ".hpp",
        ".cu", ".cuh",
        ".go", ".rs", ".zig", ".nim",
        ".s", ".asm", ".S",
        # JVM
        ".java", ".kt", ".kts", ".scala", ".sc", ".groovy",
        ".clj", ".cljs", ".cljc",
        # .NET
        ".cs", ".vb", ".fs", ".fsx", ".fsi",
        # apple
        ".swift", ".m", ".mm",
        # other major
        ".rb", ".lua", ".pl", ".pm", ".r", ".dart", ".jl",
        ".ex", ".exs", ".erl", ".hs", ".lhs", ".ml", ".mli",
        ".pas", ".f", ".f90", ".f95", ".cr",
        # shell + build
        ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh",
        # Windows shells — PowerShell (.ps1 script, .psm1 module, .psd1
        # manifest) and legacy batch (.bat/.cmd). All plain text.
        ".ps1", ".psm1", ".psd1", ".bat", ".cmd",
        ".gradle", ".cmake", ".mk", ".am", ".ac", ".m4",
        # infra / data-language
        ".tf", ".tfvars", ".hcl",
        ".nix", ".dhall", ".jsonnet", ".libsonnet",
        ".bzl", ".bazel", ".star", ".starlark",
        ".proto", ".thrift", ".fbs", ".graphql", ".gql",
        # i18n / docs assets
        ".po", ".pot",
    ]

    # Files matched by exact name (case-sensitive). These are extensionless
    # or have an unknown extension but are reliably text in real projects.
    filenames: ClassVar[set[str]] = {
        # legal / project metadata
        "LICENSE", "LICENCE", "LICENSE.txt", "LICENCE.txt",
        "NOTICE", "AUTHORS", "CONTRIBUTORS", "MAINTAINERS", "OWNERS",
        "CHANGELOG", "CHANGES", "HISTORY", "RELEASES",
        "README", "INSTALL", "TODO", "CODEOWNERS",
        "COPYING", "COPYRIGHT", "VERSION", "PATENTS",
        # python packaging
        "MANIFEST", "MANIFEST.in",
        # build / container / deploy / config
        "Makefile", "GNUmakefile", "Dockerfile", "Containerfile",
        "Procfile", "Capfile", "Vagrantfile", "Jenkinsfile", "Justfile",
        "Caddyfile", "Gemfile", "Rakefile", "Brewfile", "Pipfile",
        "BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel",
        "go.mod", "go.sum",
        # lockfiles (text — toml/json/yaml in disguise)
        "Cargo.lock", "Pipfile.lock", "poetry.lock", "uv.lock", "yarn.lock",
        "package-lock.json", "Gemfile.lock", "composer.lock",
        # config dotfiles (no extension under any reasonable interpretation)
        ".gitignore", ".gitattributes", ".gitmodules", ".mailmap",
        ".dockerignore", ".npmignore", ".prettierignore", ".eslintignore",
        ".editorconfig", ".npmrc", ".yarnrc", ".nvmrc",
        ".python-version", ".ruby-version", ".node-version",
        ".tool-versions", ".envrc", ".env",
        ".flake8", ".pylintrc", ".eslintrc", ".prettierrc",
        ".babelrc", ".browserslistrc", ".stylelintrc",
    }

    def can_parse(self, file_path: Path) -> bool:
        if file_path.name in self.filenames:
            return True
        if file_path.suffix.lower() in self.extensions:
            return True
        # Extensionless files are common from synced sources — Google Meet
        # "29 13:00 PST - Chat" exports, Fireflies notes, etc. Extension
        # matching can't see them, so for a file with NO extension at all we
        # sniff the content and accept it when it reads as UTF-8 text rather
        # than dropping it as unsupported. A file with an *unknown* extension
        # is left alone: an explicit extension we don't list is a deliberate
        # signal, whereas no extension is just missing metadata.
        if file_path.suffix == "":
            return _looks_like_text(file_path)
        return False

    def parse(self, file_path: Path) -> ParserResult:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParserResult.failure(f"text read failed: {e}")
        return ParserResult(content=content)
