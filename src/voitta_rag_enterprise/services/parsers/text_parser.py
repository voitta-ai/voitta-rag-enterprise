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


class TextParser(BaseParser):
    # Anything that's plain UTF-8 text. Files matching either ``extensions``
    # *or* ``filenames`` route through this parser; the dispatcher
    # (registry.py) hands a file to TextParser iff one of them claims it,
    # so missing entries here = silently dropped on indexing.
    extensions: ClassVar[list[str]] = [
        # plain text + docs
        ".txt", ".md", ".markdown", ".rst", ".rst_t", ".log", ".tex", ".bib",
        ".adoc", ".asciidoc", ".org", ".patch", ".diff", ".man",
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
        return file_path.suffix.lower() in self.extensions

    def parse(self, file_path: Path) -> ParserResult:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ParserResult.failure(f"text read failed: {e}")
        return ParserResult(content=content)
