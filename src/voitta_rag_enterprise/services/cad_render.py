"""On-demand CAD projection rendering.

Registers ``cad_projection`` as an asset handler. The MCP tool
:func:`request_asset` invokes :meth:`CadProjectionHandler.request`,
which mints 4 signed URLs (one per projection). The HTTP route
:meth:`api/routes/assets.fetch_asset` calls
:meth:`CadProjectionHandler.fetch`, which does the actual rendering.

No caching anywhere — the render runs in full on every fetch. The
rationale: the index is the source of truth for what slugs exist, and
the file's CAS bytes are the source of truth for what the geometry
looks like. Both can change (re-extract); a cached PNG would lie
between them. Spending ~1-3 s per render is the price of "what you see
matches what is indexed."

The headless GL backend is OSMesa (CPU). We pre-set
``PYOPENGL_PLATFORM=osmesa`` *before* pyrender is imported — pyrender
caches the backend at import time and there's no API to change it
later. EGL/GPU would be faster but requires functioning drivers on the
deploy box; OSMesa "just works" anywhere ``libosmesa6`` is installed.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from typing import Any

# pyrender reads this once at module load; setting it after import is
# a no-op. Keep this line above the import.
os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")

import numpy as np  # noqa: E402  — must follow PYOPENGL_PLATFORM
from PIL import Image as PILImage  # noqa: E402

from .asset_handlers import (  # noqa: E402
    AssetHandler,
    AssetResponse,
    RenderedAsset,
    coerce_int,
    register,
)
from .signed_assets import issue_token  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Camera setup — 4 canonical projections
# ---------------------------------------------------------------------------


# Each projection produces a 4×4 view transform that places the camera
# looking at the origin. The scene gets re-centered + scaled at render
# time so the bbox center sits at origin and the longest axis is unit
# length — these vectors then frame it consistently.
_PROJECTIONS: dict[str, tuple[float, float, float]] = {
    # (camera_x, camera_y, camera_z) → camera positions relative to origin.
    # Z is "up" in glTF's right-handed coordinate system.
    "front": (0.0, -2.5, 0.0),  # looking from -Y toward +Y
    "top":   (0.0,  0.0, 2.5),  # looking from +Z down
    "side":  (2.5,  0.0, 0.0),  # looking from +X toward -X
    "iso":   (1.75, -1.75, 1.75),  # front-right-above
}


def projection_names() -> list[str]:
    return list(_PROJECTIONS.keys())


def _look_at_origin(eye: tuple[float, float, float]) -> np.ndarray:
    """Build a 4×4 camera pose looking from ``eye`` at the origin.

    pyrender expects camera-from-world transforms with +Z as the
    "into the scene" axis from the camera's POV. We compute the
    standard look-at: forward = origin - eye, up = world-Z (or world-Y
    if that degenerates).
    """
    eye_v = np.array(eye, dtype=np.float32)
    target = np.zeros(3, dtype=np.float32)
    forward = target - eye_v
    forward /= np.linalg.norm(forward)

    # Choose an up axis that isn't parallel to forward. World Z is
    # the obvious pick except for the "top" view which looks straight
    # down — there we use world Y.
    if abs(forward[2]) > 0.95:
        world_up = np.array([0, 1, 0], dtype=np.float32)
    else:
        world_up = np.array([0, 0, 1], dtype=np.float32)

    right = np.cross(forward, world_up)
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)

    # pyrender camera pose: columns are [right, up, -forward, eye]
    pose = np.eye(4, dtype=np.float32)
    pose[:3, 0] = right
    pose[:3, 1] = up
    pose[:3, 2] = -forward
    pose[:3, 3] = eye_v
    return pose


# ---------------------------------------------------------------------------
# Subgraph isolation: pick which glTF nodes belong to a slug
# ---------------------------------------------------------------------------


# Same regexes the parser uses, duplicated here intentionally — keeps
# this module's import graph tiny (no parser dependency at render time).
_DOUBLE_COLON_RE = re.compile(r"^(?P<group>.+?)\s+::\s+(?P<part>.+)$")
_BRACKET_GROUP_RE = re.compile(r"^(?P<part>.+?)\s*\[(?P<group>[^\]]+)\]\s*$")


def _slugify(name: str, seen: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "component"
    slug = base
    n = 2
    while slug in seen:
        slug = f"{base}-{n}"
        n += 1
    seen.add(slug)
    return slug


def _route(name: str) -> str | None:
    """Return the group name a node belongs to, or None to skip."""
    if not name or name.startswith("Origin"):
        return None
    m = _DOUBLE_COLON_RE.match(name)
    if m:
        return m.group("group").strip()
    m = _BRACKET_GROUP_RE.match(name)
    if m:
        return m.group("group").strip()
    return None  # routes to "Unclassified" — handled by caller


def slug_to_group_name(scene_node_names: list[str], target_slug: str) -> str | None:
    """Reverse-resolve ``target_slug`` to the group name as the parser
    would have named it.

    We rebuild the same slugs-in-deterministic-order pass the parser
    uses. Re-running this against the *same* file bytes always yields
    the same slug→group mapping; against different bytes, we don't
    pretend — the slug just won't be found and the caller raises 404.

    Returns ``None`` if the slug doesn't map to any group in the
    current file. The caller treats that as the "stale slug" case.
    """
    from collections import Counter

    # Tally part-counts per group to get the same deterministic sort
    # the parser uses (descending parts, then name).
    counts: Counter = Counter()
    seen_groups: set[str] = set()
    for n in scene_node_names:
        g = _route(n)
        bucket = g if g is not None else "Unclassified"
        counts[bucket] += 1
        seen_groups.add(bucket)
    ordered = sorted(seen_groups, key=lambda g: (-counts[g], g.lower()))
    seen_slugs: set[str] = set()
    for group in ordered:
        slug = _slugify(group, seen_slugs)
        if slug == target_slug:
            return group
    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CadProjectionHandler(AssetHandler):
    """4-projection renderer for one component of a CAD assembly."""

    asset_type = "cad_projection"

    def params_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "size": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 2048,
                    "default": 320,
                },
            },
            "additionalProperties": False,
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        from .indexing_caps import get_caps

        max_size = getattr(get_caps(), "cad_render_max_size", 2048)
        size = coerce_int(params, "size", default=320, minimum=64, maximum=max_size)
        return {"size": size}

    def request(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
    ) -> AssetResponse:
        if not slug:
            raise ValueError("cad_projection requires a slug (component name)")

        # Mint one signed URL per projection. The ``__variant__`` key
        # rides in the token's params dict; the route's fetch path
        # peels it back off before calling fetch().
        urls: dict[str, str] = {}
        expires_at = 0
        for proj in _PROJECTIONS:
            token, exp = issue_token(
                file_id=file_id,
                asset_type=self.asset_type,
                slug=slug,
                params={**params, "__variant__": proj},
                user_id=user_id,
            )
            urls[proj] = f"/api/assets/{token}"
            expires_at = exp  # all tokens minted in the same instant
        return AssetResponse(asset_type=self.asset_type, urls=urls, expires_at=expires_at)

    def fetch(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
        variant: str | None,
    ) -> RenderedAsset:
        if not slug:
            raise ValueError("cad_projection fetch requires a slug")
        if variant not in _PROJECTIONS:
            raise ValueError(
                f"unknown projection: {variant!r}; expected one of "
                f"{sorted(_PROJECTIONS.keys())}"
            )
        size = int(params.get("size", 320))

        # Resolve the file to its on-disk glb. ``read_cas_bytes`` is the
        # natural primitive but our cas store only writes named blobs
        # under the file-cas dir — the raw source bytes live in the
        # original folder root. We read them from there.
        from ..db.database import session_scope
        from ..db.models import File, Folder

        with session_scope() as s:
            f = s.get(File, file_id)
            if f is None:
                raise FileNotFoundError(f"file {file_id} not found")
            folder = s.get(Folder, f.folder_id)
            if folder is None:
                raise FileNotFoundError(f"folder for file {file_id} missing")
            abs_path = f"{folder.path}/{f.rel_path}"

        from .indexing_caps import get_caps

        timeout_s = getattr(get_caps(), "cad_render_timeout_s", 30)
        started = time.perf_counter()

        png = _render_component(
            glb_path=abs_path,
            slug=slug,
            projection=variant,
            size=size,
            deadline=started + timeout_s,
        )
        return RenderedAsset(body=png, mime="image/png")


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------


def _render_component(
    *,
    glb_path: str,
    slug: str,
    projection: str,
    size: int,
    deadline: float,
) -> bytes:
    """Load → isolate → render → PNG.

    Each step checks the wall-clock against ``deadline`` and raises
    ``TimeoutError`` if exceeded — the route converts that into 504.
    Some scenes have millions of triangles and even the load step can
    take seconds; without the deadline a single huge file would block
    the server thread indefinitely.
    """
    import trimesh
    import pyrender

    def _expired() -> bool:
        return time.perf_counter() > deadline

    if _expired():
        raise TimeoutError("render: load deadline exceeded before start")

    try:
        scene = trimesh.load(glb_path, force="scene")
    except Exception as e:
        raise ValueError(f"glb load failed: {e}") from e
    if _expired():
        raise TimeoutError("render: load took longer than allowed")

    # Reverse the slug → group lookup using the same algorithm the
    # parser uses. trimesh's scene.graph.nodes covers every leaf
    # placement, but the slug map is built from the original glTF
    # node names; pull those from the parsed asset to keep parity.
    raw_names = _trimesh_original_node_names(scene)
    group_name = slug_to_group_name(raw_names, slug)
    if group_name is None:
        raise KeyError(f"slug {slug!r} not in current file")

    # Build the subset of trimesh-graph nodes that belong to this
    # component. trimesh expanded the gltf instances into hashed
    # variants; we route each by re-applying the regex to the
    # un-suffixed name.
    target_nodes = []
    for n in scene.graph.nodes:
        stripped = _strip_trimesh_suffix(n)
        g = _route(stripped) or "Unclassified"
        if g == group_name:
            target_nodes.append(n)
    if not target_nodes:
        raise KeyError(
            f"slug {slug!r} maps to {group_name!r} but no trimesh nodes route there"
        )
    if _expired():
        raise TimeoutError("render: subgraph extraction exceeded deadline")

    # Build a fresh trimesh.Scene containing only those node names'
    # geometry, with their world transforms baked in. Bake matters:
    # the parent transform chain disappears when we extract a single
    # node's mesh, so we need to apply the cumulative transform from
    # world to leaf at copy time.
    sub_scene = trimesh.Scene()
    for n in target_nodes:
        try:
            transform, geometry_name = scene.graph[n]
        except KeyError:
            continue
        if geometry_name is None or geometry_name not in scene.geometry:
            continue
        geom = scene.geometry[geometry_name]
        sub_scene.add_geometry(geom, node_name=n, transform=transform)
    if not sub_scene.geometry:
        raise KeyError(
            f"slug {slug!r} mapped to {group_name!r} has no renderable geometry"
        )
    if _expired():
        raise TimeoutError("render: subgraph build exceeded deadline")

    # Re-center + uniform-scale so the component fits the canonical
    # [-0.5, 0.5] box. This makes the camera vectors above frame the
    # subject consistently regardless of the original units.
    bounds = sub_scene.bounds
    if bounds is None or not np.all(np.isfinite(bounds)):
        raise ValueError("subgraph has no finite bounding box")
    center = (bounds[0] + bounds[1]) / 2.0
    extent = bounds[1] - bounds[0]
    longest = float(max(extent))
    if longest <= 0:
        raise ValueError("subgraph extent is zero")
    scale = 1.0 / longest
    T = np.eye(4, dtype=np.float32)
    T[:3, :3] *= scale
    T[:3, 3] = -center * scale
    sub_scene.apply_transform(T)

    # Build a pyrender scene from the trimesh scene.
    py_scene = pyrender.Scene(
        bg_color=[1.0, 1.0, 1.0, 1.0],
        ambient_light=[0.4, 0.4, 0.4],
    )
    for trimesh_geom in sub_scene.dump():
        mesh = pyrender.Mesh.from_trimesh(trimesh_geom, smooth=False)
        py_scene.add(mesh)

    # One directional light keyed roughly to the viewer direction.
    light = pyrender.DirectionalLight(color=np.ones(3), intensity=3.0)
    py_scene.add(light, pose=_look_at_origin((1.0, -1.0, 1.0)))

    # Camera: orthographic for axis-aligned views, perspective for iso.
    eye = _PROJECTIONS[projection]
    if projection == "iso":
        cam = pyrender.PerspectiveCamera(yfov=np.pi / 4.0, aspectRatio=1.0)
    else:
        cam = pyrender.OrthographicCamera(xmag=0.6, ymag=0.6)
    py_scene.add(cam, pose=_look_at_origin(eye))
    if _expired():
        raise TimeoutError("render: scene build exceeded deadline")

    # Offscreen render.
    renderer = pyrender.OffscreenRenderer(viewport_width=size, viewport_height=size)
    try:
        color, _depth = renderer.render(py_scene)
    finally:
        renderer.delete()

    img = PILImage.fromarray(color)
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _trimesh_original_node_names(scene) -> list[str]:
    """Pull the underlying glTF node names from a trimesh Scene.

    trimesh remembers the source names in ``scene.metadata['nodes']``
    when available (newer versions). When not, we fall back to the
    graph node names with the per-instance suffix stripped — slightly
    lossy but enough to drive ``slug_to_group_name``.
    """
    md = getattr(scene, "metadata", {}) or {}
    nodes = md.get("nodes")
    if isinstance(nodes, list) and nodes and isinstance(nodes[0], dict):
        return [n.get("name", "") for n in nodes if isinstance(n, dict)]
    return [_strip_trimesh_suffix(n) for n in scene.graph.nodes]


_TRIMESH_HEX_RE = re.compile(r"(?:_[0-9a-f]{6,})+$")
_TRIMESH_NUM_RE = re.compile(r"(?:_\d{1,3})+$")


def _strip_trimesh_suffix(name: str) -> str:
    out = name
    for _ in range(3):
        before = out
        out = _TRIMESH_HEX_RE.sub("", out)
        out = _TRIMESH_NUM_RE.sub("", out)
        out = out.rstrip("_")
        if out == before:
            break
    return out


# ---------------------------------------------------------------------------
# Boot-time registration
# ---------------------------------------------------------------------------


_HANDLER = CadProjectionHandler()
register(_HANDLER)
