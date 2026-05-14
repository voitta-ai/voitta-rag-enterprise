"""Pass-through asset: serve the original (un-extracted) file bytes.

Registers ``asset_type="original"`` with the on-demand asset framework.
Returns one signed URL — same machinery as ``cad_projection`` — that
streams the file's source bytes back: the actual PDF, DOCX, XLSX,
STEP, ZIP, whatever was indexed.

Why this exists
---------------
Every other read-side MCP tool surfaces a *derived* view: extracted
markdown (``get_file``), per-page renders (``get_page_image``), the
spreadsheet's reflowed-to-markdown summary (``get_chunk_range`` on
Sheets-derived files). Some downstream pipelines — most notably the
voitta-bookmarklet's chat backend, where the LLM hands a tool a
``python_storage`` handle and runs a Python script over the bytes —
need the original *file*, not a Voitta-flavoured extract.

The handler is intentionally tiny: there's no rendering, no
re-parsing, no caching layer. The HTTP fetch path reads the file
straight from disk (same path the indexer reads from on every sync)
and streams it back with a best-effort MIME guess.

Path resolution
---------------
File rows store ``folder.path`` (the absolute root of the folder
ACL'd to the user, both for ``source_type="filesystem"`` AND for
the local cache that connectors like Google Drive sync into) and
``file.rel_path`` (within the folder). The original sits at
``{folder.path}/{file.rel_path}`` — same convention every other
read-from-disk caller uses (see ``cad_render.fetch``).

Security
--------
Same model as every other asset: the URL is the credential, signed
with the server's secret, TTL'd. The ACL gate runs at *mint time*
inside ``mcp_server.request_asset`` — the caller has to be able to
see the file already, which means the same per-user file
visibility ``search`` / ``get_file`` enforce. The HTTP fetch path
trusts a valid signature.

There's deliberately no ``slug`` and no parameters — "give me the
original" doesn't have variants. Callers that want a different
view (page render, projection) use the asset_type that fits.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from .asset_handlers import (
    AssetHandler,
    AssetResponse,
    AssetSpec,
    RenderedAsset,
    register,
)
from .signed_assets import issue_token

logger = logging.getLogger(__name__)

ASSET_TYPE = "original"

# Mime guesses for extensions the stdlib doesn't already know. Stdlib
# gets PDF / DOCX / XLSX / PNG / etc. right; this table covers the
# engineering-pipeline formats we routinely index.
_EXTRA_MIME: dict[str, str] = {
    ".step": "model/step",
    ".stp": "model/step",
    ".iges": "model/iges",
    ".igs": "model/iges",
    ".fcstd": "application/x-extension-fcstd",
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".gltf": "model/gltf+json",
    ".glb": "model/gltf-binary",
    ".ipynb": "application/x-ipynb+json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".log": "text/plain",
}


def guess_mime(rel_path: str) -> str:
    """Best-effort MIME guess. Falls back to ``application/octet-stream``."""
    suffix = Path(rel_path).suffix.lower()
    extra = _EXTRA_MIME.get(suffix)
    if extra:
        return extra
    guess, _ = mimetypes.guess_type(rel_path)
    return guess or "application/octet-stream"


class OriginalFileHandler(AssetHandler):
    """Stream the original file bytes via a signed URL.

    Single-variant (``"file"``) so the response shape stays uniform
    with multi-variant handlers like ``cad_projection``. The MCP
    wrapper hands the LLM a dict like ``{"urls": {"file": "..."},
    "expires_at": …}`` regardless.
    """

    asset_type = ASSET_TYPE

    def params_schema(self) -> dict[str, Any]:
        # No parameters today. Kept as an empty object (not omitted)
        # so the LLM gets an explicit "this is parameter-less" signal
        # via ``list_assets``.
        return {"type": "object", "additionalProperties": False}

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        # Pass-through. Any junk in params is ignored at this layer;
        # we don't need them.
        return {}

    def request(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
    ) -> AssetResponse:
        # Pre-flight: confirm the file exists on disk *now*, so the
        # LLM gets the failure inline (as a tool error) instead of a
        # 410 surprise later when it fetches the URL. This is cheap
        # — one DB hit + one stat — and saves a round-trip on the
        # common "Drive folder un-synced since indexing" case.
        from ..db.database import session_scope
        from ..db.models import File, Folder

        with session_scope() as s:
            f = s.get(File, file_id)
            if f is None:
                raise FileNotFoundError(f"file {file_id} not found")
            folder = s.get(Folder, f.folder_id)
            if folder is None:
                raise FileNotFoundError(
                    f"folder for file {file_id} missing"
                )
            abs_path = Path(folder.path) / f.rel_path
            rel_path = f.rel_path

        if not abs_path.is_file():
            raise FileNotFoundError(
                f"original bytes for file {file_id} ({rel_path}) "
                f"not on disk at {abs_path}"
            )

        from ..config import get_settings

        settings = get_settings()
        token, expires_at = issue_token(
            file_id=file_id,
            asset_type=self.asset_type,
            slug=None,
            params={"__variant__": "file"},
            user_id=user_id,
        )
        return AssetResponse(
            asset_type=self.asset_type,
            urls={"file": settings.asset_url(token)},
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
        # The fetch path runs out-of-band (signed URL hit by the
        # client). Re-resolve from the DB rather than trusting the
        # token's encoded params — the file may have been deleted /
        # rebuilt since the URL was minted. Returning 410 in those
        # cases is the right behaviour.
        from ..db.database import session_scope
        from ..db.models import File, Folder

        with session_scope() as s:
            f = s.get(File, file_id)
            if f is None:
                raise FileNotFoundError(f"file {file_id} not found")
            folder = s.get(Folder, f.folder_id)
            if folder is None:
                raise FileNotFoundError(
                    f"folder for file {file_id} missing"
                )
            abs_path = Path(folder.path) / f.rel_path
            rel_path = f.rel_path

        if not abs_path.is_file():
            raise FileNotFoundError(
                f"original bytes for file {file_id} ({rel_path}) "
                f"not on disk at {abs_path}"
            )

        # Single-shot read. Indexed files are typically O(MB); the
        # CAD pipeline already reads 35 MB STEPs into memory for
        # parsing, so the precedent is set. If we ever want to stream
        # huge files, that's a Response refactor, not a handler
        # refactor — until then, simple wins.
        try:
            body = abs_path.read_bytes()
        except OSError as e:
            # Permissions / mid-sync read collisions / disk eviction.
            # 410 is the right HTTP code: the bytes existed when we
            # minted the URL (the request() preflight stat'd them),
            # but they're gone now.
            raise FileNotFoundError(
                f"could not read original bytes for file {file_id} "
                f"({rel_path}): {e}"
            ) from e

        return RenderedAsset(body=body, mime=guess_mime(rel_path))


_HANDLER = OriginalFileHandler()
register(_HANDLER)


def spec_for(file_id: int, rel_path: str | None) -> AssetSpec:
    """Build the menu entry for ``list_assets``.

    Synthetic: every indexed file gets one, no parser involvement.
    The label includes the filename when known so the LLM can
    confirm at a glance that it's asking for the right file.
    """
    name = Path(rel_path).name if rel_path else f"file {file_id}"
    return AssetSpec(
        asset_type=ASSET_TYPE,
        label=f"Original bytes ({name})",
        description=(
            "Return the file's original, un-extracted bytes via a "
            "signed URL. The URL is single-use within its TTL and "
            "carries no in-band auth — the URL itself is the "
            "credential. Mime is best-effort from the file "
            "extension. Use this when you need the source format "
            "(PDF/DOCX/XLSX/STEP/...) and not the markdown extract; "
            "the extract is what ``get_file`` returns."
        ),
        slug=None,
        params_schema={"type": "object", "additionalProperties": False},
        examples=(
            {"asset_type": ASSET_TYPE, "slug": None, "params": {}},
        ),
    )
