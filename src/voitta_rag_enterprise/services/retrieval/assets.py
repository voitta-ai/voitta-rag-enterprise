"""On-demand derived assets of a file (original bytes, markdown, CAD views…)."""

from __future__ import annotations

from ...db.database import session_scope
from .viewer import Viewer


def list_assets(viewer: Viewer, file_id: int) -> list[dict]:
    """Asset specs the file exposes: the synthetic ``original`` / ``md`` /
    ``cad_mesh`` entries plus whatever the parser declared at index time."""
    from .. import asset_handlers as _ah
    from .. import cad_mesh as _cm
    from .. import markdown_extract as _md
    from .. import original_file as _orig

    with session_scope() as s:
        f = viewer.require_file(s, file_id)
        cas_id = f.file_cas_id
        rel_path = f.rel_path
    # "original" first — the common case is "give me the bytes".
    out: list[dict] = [_orig.spec_for(file_id, rel_path).as_dict()]
    # "md" only when the CAS extract actually exists.
    md_spec = _md.spec_for(file_id, cas_id, rel_path)
    if md_spec is not None:
        out.append(md_spec.as_dict())
    # Whole-file GLB for CAD files; per-component export reuses the
    # cad_projection slugs rather than ballooning this menu.
    if _cm.is_cad_file(rel_path):
        out.append(_cm.spec_for(file_id, rel_path).as_dict())
    out.extend(spec.as_dict() for spec in _ah.load_assets_for_file(cas_id))
    return out


def request_asset(
    viewer: Viewer,
    file_id: int,
    asset_type: str,
    slug: str | None = None,
    params: dict | None = None,
) -> dict:
    """Produce an asset: ``inline`` data or signed ``urls`` (+ ``expires_at``)."""
    from .. import asset_handlers as _ah

    try:
        handler = _ah.get_handler(asset_type)
    except KeyError as e:
        raise ValueError(f"unknown asset_type: {asset_type!r}") from e

    # Handlers raise ValueError on bad input; that surfaces to the LLM verbatim.
    validated = handler.validate_params(dict(params or {}))

    # Authorize BEFORE minting — the returned URL is HMAC-signed and IS the
    # credential (the /api/assets endpoint carries no identity auth), so a
    # caller who can't see the file must never receive a URL for it.
    with session_scope() as s:
        viewer.require_file(s, file_id)
    response = handler.request(
        file_id=file_id,
        slug=slug,
        params=validated,
        user_id=viewer.user_id,
    )
    out: dict = {"asset_type": response.asset_type}
    if response.inline is not None:
        out["inline"] = response.inline
    if response.urls is not None:
        out["urls"] = response.urls
        if response.expires_at is not None:
            out["expires_at"] = response.expires_at
    return out
