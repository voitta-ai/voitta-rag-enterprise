"""Asset-handler registry.

A *handler* is any callable that produces a derived view of a file when
the LLM asks for it via the ``request_asset`` MCP tool. Examples:

* ``cad_projection`` — render 4 projections of one CAD component
* ``chart_image`` — rasterize a chart from an xlsx sheet (future)
* ``data_query`` — run a SQL/JMESPath against a tabular file (future)
* ``page_image_hires`` — re-render a PDF page at higher resolution (future)

The registry maps ``asset_type`` strings to ``AssetHandler`` instances.
Handlers register themselves at import time via :func:`register`;
``main.py`` triggers the imports during app startup so the registry is
populated by the time the HTTP / MCP routes start serving.

Single source of truth for:

* The list of available asset types (used by ``list_assets``).
* Per-type params validation (each handler declares a ``params_schema``;
  the route + MCP wrapper run :func:`validate_params` before any
  expensive work).
* The dispatch table that turns a signed token into bytes / JSON.

Why the indirection — why not just call handlers directly? Because we
want the HTTP route, the MCP tool, and the tests to share one code path
and one validation pass. Registry + dispatch keeps each handler module
self-contained: it imports the registry, declares itself, and the rest
of the app discovers it by string name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetSpec:
    """Self-describing menu entry the LLM consumes via ``list_assets``.

    Parsers attach a list of ``AssetSpec`` to their ``ParserResult`` to
    describe the on-demand renders / queries the file supports. The
    same shape is returned from ``list_assets(file_id)``, possibly
    augmented with global asset types not tied to any one parser.

    ``slug`` identifies a within-file target (component, sheet, page).
    Some asset types (whole-file operations like ``data_query``) don't
    need one — leave it None.

    ``params_schema`` is a JSON Schema fragment, so the LLM can read it
    and construct valid ``params`` dicts. We don't enforce a full JSON
    Schema validator — handlers do their own pydantic-style validation
    in :meth:`AssetHandler.invoke`. The schema is documentation +
    structure hints.
    """

    asset_type: str
    label: str
    description: str
    slug: str | None = None
    params_schema: dict[str, Any] | None = None
    examples: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_type": self.asset_type,
            "label": self.label,
            "description": self.description,
            "slug": self.slug,
            "params_schema": self.params_schema or {},
            "examples": list(self.examples),
        }


@dataclass
class AssetResponse:
    """Handler output.

    Exactly one of ``inline`` or ``urls`` should be populated:

    * ``inline`` — structured data the LLM consumes directly (rows from
      a query, summary statistics, …). The MCP wrapper passes this
      through verbatim; no signed URL is involved.

    * ``urls`` — variant_name → signed URL. The LLM follows each URL
      with its bearer token to fetch bytes. ``expires_at`` should be
      populated alongside.

    The mixed shape is intentional: data-flavored assets and render-
    flavored assets share one channel.
    """

    asset_type: str
    inline: dict[str, Any] | None = None
    urls: dict[str, str] | None = None
    expires_at: int | None = None


class AssetHandler:
    """Abstract handler. Implementations register themselves via
    :func:`register` at module-import time."""

    asset_type: str = ""

    def params_schema(self) -> dict[str, Any]:
        """JSON Schema for ``params``. Override to declare structure.

        Default: empty object, no params required."""
        return {"type": "object", "additionalProperties": True}

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Hook for cheap type-coercion + bounds checks.

        Implementations should raise ``ValueError`` with a clear
        message for invalid input. The caller (HTTP route or MCP
        wrapper) turns that into 400.

        Default: pass through unchanged. Override to do real work."""
        return dict(params or {})

    def request(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
    ) -> "AssetResponse":
        """Called when the LLM invokes ``request_asset``.

        Two paths:

        1. **Data-flavored** — the handler computes the answer right
           here and returns ``AssetResponse(inline=...)``. No token is
           issued.

        2. **Render-flavored** — the handler mints one or more signed
           tokens via :func:`signed_assets.issue_token`, packages them
           into ``urls=`` keyed by variant name, returns
           ``AssetResponse(urls=..., expires_at=...)``.

        Implementations may also pre-validate that the slug exists,
        the parameters are sensible, etc. before issuing tokens — the
        HTTP fetch path also re-validates, but it's cheaper to fail
        fast in the MCP call than after a URL has been fetched.
        """
        raise NotImplementedError

    def fetch(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
        variant: str | None,
    ) -> "RenderedAsset":
        """Called when the HTTP route resolves a signed URL into bytes.

        Only render-flavored handlers implement this; data-flavored
        handlers should leave the default :class:`NotImplementedError`
        in place, since their ``request`` already returned inline data
        and no URL was minted.

        The ``variant`` argument disambiguates which view to produce
        when ``urls`` contained multiple entries (e.g. ``"front"``,
        ``"top"``, ``"side"``, ``"iso"`` for CAD projections). The
        variant string is part of the URL path and is round-tripped
        through the token's params dict — see ``api/routes/assets.py``.
        """
        raise NotImplementedError


@dataclass
class RenderedAsset:
    """Bytes + mime for the HTTP fetch path.

    ``filename`` is optional. When set, the HTTP route surfaces it as
    ``Content-Disposition: attachment; filename="<name>"`` so a downstream
    client (browser, ``curl -OJ``, the bookmarklet's MCP adapter writing
    into python_storage) gets a human-recognisable name without having
    to derive one from the URL token.  Leave it ``None`` for renders
    that don't have a meaningful source name (synthesised projections,
    cropped figures); the route falls back to inline disposition with
    just the MIME.
    """

    body: bytes
    mime: str
    filename: str | None = None


_HANDLERS: dict[str, AssetHandler] = {}


def register(handler: AssetHandler) -> AssetHandler:
    """Register ``handler`` under its declared ``asset_type``.

    Idempotent on re-import (replacing a registration with itself is a
    no-op). Raises ``ValueError`` only if a *different* handler
    instance tries to claim the same asset_type — that's a
    misconfiguration we want loud at boot, not at first request.
    """
    if not handler.asset_type:
        raise ValueError(f"{type(handler).__name__} has no asset_type")
    existing = _HANDLERS.get(handler.asset_type)
    if existing is not None and existing is not handler:
        # Allow replacement only when classes match — useful for tests
        # that re-import handler modules. A genuinely different class
        # claiming the same name is a name clash and we want to error.
        if type(existing) is not type(handler):
            raise ValueError(
                f"asset_type {handler.asset_type!r} already registered by "
                f"{type(existing).__name__}; can't register {type(handler).__name__}"
            )
    _HANDLERS[handler.asset_type] = handler
    logger.info("registered asset handler: %s", handler.asset_type)
    return handler


def get_handler(asset_type: str) -> AssetHandler:
    """Look up a registered handler. Raises ``KeyError`` if missing."""
    h = _HANDLERS.get(asset_type)
    if h is None:
        raise KeyError(asset_type)
    return h


def all_handlers() -> dict[str, AssetHandler]:
    """Snapshot of the registry — used by ``list_assets`` to surface
    handlers that apply to a file independent of what its parser
    declared. Returns a copy so callers can't mutate the registry."""
    return dict(_HANDLERS)


def reset_for_tests() -> None:
    """Test helper: clear the registry. Production code never calls this."""
    _HANDLERS.clear()


# ---------------------------------------------------------------------------
# Built-in helpers
# ---------------------------------------------------------------------------


# A common bound-checking helper handlers reach for. Centralized so a
# new handler that wants a ``size`` parameter does the same thing as
# every other ``size`` parameter in the system.
def coerce_int(
    params: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = params.get(name, default)
    try:
        v = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}: expected integer, got {raw!r}") from e
    if v < minimum or v > maximum:
        raise ValueError(
            f"{name}: {v} out of range [{minimum}, {maximum}]"
        )
    return v


# ---------------------------------------------------------------------------
# Per-file menu lookup
# ---------------------------------------------------------------------------


def load_assets_for_file(file_cas_id: str | None) -> list[AssetSpec]:
    """Read the per-file asset menu from ``cas/files/<sha>/on_demand_assets.json``.

    Empty list when:
    * the file has never been indexed (no CAS row),
    * the parser declared no on-demand assets (no file written),
    * the JSON is malformed (logged + returned empty so the LLM gets
      "menu unavailable" instead of an exception).

    Concrete asset specs are reconstructed from their dict form. Any
    field not on the dataclass is dropped silently — that's the
    forward-compat path for older indexed files.
    """
    if not file_cas_id:
        return []
    from ..cas import store as cas_store

    try:
        raw = cas_store.read_file_blob(file_cas_id, "on_demand_assets.json")
    except FileNotFoundError:
        return []
    try:
        import json as _json

        rows = _json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.exception(
            "malformed on_demand_assets.json for cas=%s — treating as empty",
            file_cas_id,
        )
        return []
    if not isinstance(rows, list):
        return []
    out: list[AssetSpec] = []
    known = AssetSpec.__dataclass_fields__.keys()
    for row in rows:
        if not isinstance(row, dict):
            continue
        cleaned = {k: row[k] for k in row if k in known}
        examples = cleaned.get("examples") or ()
        if isinstance(examples, list):
            cleaned["examples"] = tuple(examples)
        try:
            out.append(AssetSpec(**cleaned))
        except TypeError:
            logger.warning("skipping malformed asset spec: %r", row)
    return out
