"""Asset handler: serve the parser's markdown extract.

Registers ``asset_type="md"``. Every parser (PDF, DOCX, XLSX, PPTX,
ipynb, image, svg, plain text, Google Workspace exports synced into
the index, …) writes its normalised markdown to
``cas/files/<file_sha>/text.md`` during indexing
(``services.indexing._index_one``). This handler streams that exact
blob back via a signed URL — same machinery as ``original``, just a
different source.

Why this exists
---------------
``get_file`` returns the same markdown but as a tool-call payload —
the bytes pass through the LLM's reasoning context. For bulk work
(regex over a long DOCX, dataframe assembly from an XLSX extract,
grep-style passes over PPTX speaker notes) the LLM should hand the
markdown to ``run_compute`` via ``python_storage`` instead. Mirrors
the ``original`` pattern, but pre-extracted:

    asset = request_asset(file_id=N, asset_type="md")
    snap  = fetch_to_python_storage(
                url=asset["urls"]["md"],
                name="<filename>.md",
            )
    run_compute(code=f"rec = ctx.snapshot({snap['handle']!r}); ...")

Use ``original`` when you need the source format (custom parser,
preserve layout); use ``md`` when you want the already-extracted
text and the parser's output is good enough.

Availability
------------
Synthesised in ``list_assets`` whenever the file has a
``file_cas_id`` *and* a ``text.md`` blob exists in CAS. Empty
extracts (zero-byte ``text.md``) still show up — a zero-content
parse is a legitimate indexing outcome (image with no OCR text,
empty spreadsheet) and the LLM is allowed to discover that.

Security
--------
Identical model to ``original``: URL is the credential, signed and
TTL'd. ACL gate runs at request mint time via the surrounding
``request_asset`` MCP wrapper.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..cas import store as cas_store
from .asset_handlers import (
    AssetHandler,
    AssetResponse,
    AssetSpec,
    RenderedAsset,
    register,
)
from .signed_assets import issue_token

logger = logging.getLogger(__name__)

ASSET_TYPE = "md"

_BLOB_NAME = "text.md"
_MIME = "text/markdown; charset=utf-8"


def _has_md_blob(file_cas_id: str | None) -> bool:
    if not file_cas_id:
        return False
    return (cas_store.file_dir(file_cas_id) / _BLOB_NAME).is_file()


class MarkdownExtractHandler(AssetHandler):
    """Stream ``cas/files/<sha>/text.md`` via a signed URL.

    Single-variant (``"md"``) to keep the response shape consistent
    with other handlers. No slug (the markdown is per-file; sheet-
    level granularity is already handled by the indexer giving each
    sheet its own ``file_id``). No params.
    """

    asset_type = ASSET_TYPE

    def params_schema(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False}

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _resolve_cas_id(self, file_id: int) -> tuple[str, str]:
        """Return ``(file_cas_id, rel_path)`` or raise."""
        from ..db.database import session_scope
        from ..db.models import File

        with session_scope() as s:
            f = s.get(File, file_id)
            if f is None:
                raise FileNotFoundError(f"file {file_id} not found")
            if not f.file_cas_id:
                # File row exists but indexer never wrote a CAS entry —
                # typically means indexing failed or hasn't run yet.
                raise FileNotFoundError(
                    f"file {file_id} ({f.rel_path}) has no CAS entry; "
                    f"the parser hasn't run or the file isn't indexed yet"
                )
            return f.file_cas_id, f.rel_path

    def request(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
    ) -> AssetResponse:
        cas_id, rel_path = self._resolve_cas_id(file_id)
        if not _has_md_blob(cas_id):
            # Mirror the original handler's "fail loud at mint time"
            # so the LLM sees the error inline, not after fetching.
            raise FileNotFoundError(
                f"no markdown extract for file {file_id} ({rel_path}); "
                f"the parser produced no text.md blob"
            )

        from ..config import get_settings

        settings = get_settings()
        token, expires_at = issue_token(
            file_id=file_id,
            asset_type=self.asset_type,
            slug=None,
            params={"__variant__": "md"},
            user_id=user_id,
        )
        return AssetResponse(
            asset_type=self.asset_type,
            urls={"md": settings.asset_url(token)},
            expires_at=expires_at,
        )

    def fetch(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
        variant: str | None,
    ) -> RenderedAsset:
        # Re-resolve from the DB — the file may have been re-indexed,
        # changing file_cas_id, since the URL was minted.
        cas_id, rel_path = self._resolve_cas_id(file_id)
        try:
            body = cas_store.read_file_blob(cas_id, _BLOB_NAME)
        except OSError as e:
            raise FileNotFoundError(
                f"could not read markdown extract for file {file_id} "
                f"({rel_path}): {e}"
            ) from e
        return RenderedAsset(body=body, mime=_MIME)


_HANDLER = MarkdownExtractHandler()
register(_HANDLER)


def spec_for(
    file_id: int, file_cas_id: str | None, rel_path: str | None
) -> AssetSpec | None:
    """Build the menu entry for ``list_assets``.

    Returns ``None`` when the file has no ``text.md`` blob (image
    without OCR fallback, indexing failure, file not yet indexed) so
    ``list_assets`` simply omits the entry rather than advertising
    something that would 404.
    """
    if not _has_md_blob(file_cas_id):
        return None
    name = Path(rel_path).name if rel_path else f"file {file_id}"
    return AssetSpec(
        asset_type=ASSET_TYPE,
        label=f"Markdown extract ({name})",
        description=(
            "Return the parser's normalised markdown extract via a "
            "signed URL (``text/markdown``). Same content "
            "``get_file`` returns, but delivered as a fetchable URL "
            "so the LLM can pipe it into ``fetch_to_python_storage`` "
            "→ ``run_compute`` without putting the bytes through "
            "tool-result context. Available whenever indexing "
            "produced a text.md blob — PDF, DOCX, XLSX, PPTX, "
            "ipynb, plain text, and any Google Workspace files "
            "synced into the index. Use ``original`` instead when "
            "you need the source format."
        ),
        slug=None,
        params_schema={"type": "object", "additionalProperties": False},
        examples=(
            {"asset_type": ASSET_TYPE, "slug": None, "params": {}},
        ),
    )
