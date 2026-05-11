"""On-demand CAD projection rendering for STEP files.

Registers ``cad_projection`` with the asset-handler framework. The
MCP ``request_asset`` call mints four signed URLs (front / top / side
/ iso); each URL's HTTP fetch path lands here and produces a fresh
PNG.

Pipeline per fetch:

1. Re-parse the STEP file via OCP (deliberate; no caching anywhere)
2. Resolve the requested ``slug`` to the XCAF entry paths the parser
   stored at index time, then to TopoDS_Shape handles
3. Build a single ``TopoDS_Compound`` from those shapes
4. Tessellate via ``BRepMesh_IncrementalMesh``
5. Convert triangles to a ``vtkPolyData``
6. Render offscreen via VTK (its bundled software backend works
   without GPU / X11 / EGL)
7. Encode as PNG

The 35 MB sample STEP parses in ~13 s. Per-component tessellation
adds ~0.5 s; render adds ~0.5 s. Worst-case fetch is well under the
30 s ``cad_render_timeout_s`` cap.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .asset_handlers import (
    AssetHandler,
    AssetResponse,
    RenderedAsset,
    coerce_int,
    load_assets_for_file,
    register,
)
from .signed_assets import issue_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Camera presets
# ---------------------------------------------------------------------------


# For axis-aligned views VTK gets a vector along which the camera
# looks at the centred component; for ISO we pick a 35°-elevation,
# 30°-azimuth offset (standard mechanical-drawing convention).
_VIEWS: dict[str, dict[str, Any]] = {
    "front": {"position": (0.0, -1.0, 0.0), "up": (0.0, 0.0, 1.0), "ortho": True},
    "top":   {"position": (0.0,  0.0, 1.0), "up": (0.0, 1.0, 0.0), "ortho": True},
    "side":  {"position": (1.0,  0.0, 0.0), "up": (0.0, 0.0, 1.0), "ortho": True},
    "iso":   {"position": (1.0, -1.0, 1.0), "up": (0.0, 0.0, 1.0), "ortho": False},
}


def projection_names() -> list[str]:
    return list(_VIEWS.keys())


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CadProjectionHandler(AssetHandler):
    """4-projection renderer for one STEP component."""

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
        from ..config import get_settings

        settings = get_settings()
        urls: dict[str, str] = {}
        expires_at = 0
        for proj in _VIEWS:
            token, exp = issue_token(
                file_id=file_id,
                asset_type=self.asset_type,
                slug=slug,
                params={**params, "__variant__": proj},
                user_id=user_id,
            )
            urls[proj] = settings.asset_url(token)
            expires_at = exp
        return AssetResponse(
            asset_type=self.asset_type,
            urls=urls,
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
        if not slug:
            raise ValueError("cad_projection fetch requires a slug")
        if variant not in _VIEWS:
            raise ValueError(
                f"unknown projection: {variant!r}; expected one of "
                f"{sorted(_VIEWS.keys())}"
            )
        size = int(params.get("size", 320))

        # File-id → on-disk path + cas-id.
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
            cas_id = f.file_cas_id

        # Find the spec matching this slug. Two render code paths
        # share the same asset_type: STEP (carries ``x-entry-paths``)
        # and FCStd (carries ``x-fcstd-members``). The presence of one
        # vs the other tells us which loader to invoke.
        spec_schema = _spec_schema_for_slug(cas_id, slug)
        if spec_schema is None:
            raise KeyError(
                f"slug {slug!r} not in current asset menu for file {file_id}"
            )

        from .indexing_caps import get_caps

        timeout_s = getattr(get_caps(), "cad_render_timeout_s", 30)
        started = time.perf_counter()
        deadline = started + timeout_s

        if "x-fcstd-members" in spec_schema:
            png = _render_fcstd_component(
                fcstd_path=abs_path,
                members=spec_schema["x-fcstd-members"],
                projection=variant,
                size=size,
                deadline=deadline,
            )
        elif "x-entry-paths" in spec_schema:
            png = _render_subshape(
                step_path=abs_path,
                entry_paths=[str(e) for e in spec_schema["x-entry-paths"]],
                projection=variant,
                size=size,
                deadline=deadline,
            )
        else:
            raise KeyError(
                f"slug {slug!r} carries no recognized render hint "
                f"(neither x-entry-paths nor x-fcstd-members)"
            )
        return RenderedAsset(body=png, mime="image/png")


def _spec_schema_for_slug(cas_id: str | None, slug: str) -> dict | None:
    """Return the full ``params_schema`` for ``slug``, or None if no
    matching asset spec is found in the file's menu."""
    specs = load_assets_for_file(cas_id)
    for spec in specs:
        if spec.asset_type == "cad_projection" and spec.slug == slug:
            return spec.params_schema or {}
    return None


# ---------------------------------------------------------------------------
# Render primitives (OCP + VTK)
# ---------------------------------------------------------------------------


def _render_subshape(
    *,
    step_path: str,
    entry_paths: list[str],
    projection: str,
    size: int,
    deadline: float,
) -> bytes:
    """Parse → resolve subshape → tessellate → render → PNG.

    Every step rechecks the wall-clock against ``deadline``; running
    past it raises ``TimeoutError`` which the HTTP route maps to 504.
    A pathological STEP that takes 60 s to parse won't pin our
    request thread indefinitely.
    """
    if time.perf_counter() > deadline:
        raise TimeoutError("render: deadline exceeded before start")

    # Parse fresh — no document caching.
    from .parsers.cad_step_parser import _parse_step_into_xcaf

    doc, _schema = _parse_step_into_xcaf(__import__("pathlib").Path(step_path))
    if time.perf_counter() > deadline:
        raise TimeoutError("render: STEP parse exceeded deadline")

    # Resolve every entry path back to a label, collect their shapes
    # into a single TopoDS_Compound. The compound makes tessellation +
    # camera framing trivial since we hand VTK one polydata.
    from OCP.BRep import BRep_Builder
    from OCP.TCollection import TCollection_AsciiString
    from OCP.TDF import TDF_Label, TDF_Tool
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    data = doc.GetData()

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    resolved = 0
    for entry in entry_paths:
        label = TDF_Label()
        TDF_Tool.Label_s(data, TCollection_AsciiString(entry), label, False)
        if label.IsNull():
            continue
        shape = shape_tool.GetShape_s(label)
        if shape.IsNull():
            continue
        builder.Add(compound, shape)
        resolved += 1
    if resolved == 0:
        raise KeyError(
            "render: none of the entry paths resolved to geometry — "
            "STEP may have been re-uploaded since the menu was built"
        )
    if time.perf_counter() > deadline:
        raise TimeoutError("render: shape resolution exceeded deadline")

    # Tessellate the compound. Tolerances picked from the Creality
    # ``libslic3r/Format/STEP.cpp`` defaults: linear 0.5 mm, angular
    # 0.5 rad, parallel on. Loose enough that big assemblies finish
    # quickly; tight enough that 320-px renders look clean.
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    BRepMesh_IncrementalMesh(compound, 0.5, False, 0.5, True)
    if time.perf_counter() > deadline:
        raise TimeoutError("render: tessellation exceeded deadline")

    # Triangles → vtkPolyData. We accumulate into bulk numpy arrays
    # and then push them into VTK in one shot — much faster than
    # InsertNextCell per triangle on large compounds (sub-second for
    # ~100k triangles vs minutes).
    poly = _compound_to_polydata(compound)
    if time.perf_counter() > deadline:
        raise TimeoutError("render: polydata build exceeded deadline")

    return _render_polydata(poly, projection=projection, size=size)


def _render_fcstd_component(
    *,
    fcstd_path: str,
    members: list[dict],
    projection: str,
    size: int,
    deadline: float,
) -> bytes:
    """FCStd render path.

    Each ``member`` is ``{"brp": "<zip-internal path>"}``. World
    transforms are *not* stored in the asset spec — they're composed
    here from a fresh Document.xml parse. The trade is ~50 ms / render
    on a ~3 MB Document.xml (negligible vs tessellate+render) in
    exchange for transform-formula bugs being code-only fixes that
    don't require re-indexing every FCStd ever uploaded.

    Pipeline:
    1. Parse Document.xml → ``_Obj`` map (parent links resolved).
    2. For every requested brp, derive the Part::Feature name
       (``<name>.Shape.brp`` → ``<name>``) and walk the App::Part
       parent chain to compose the world transform. Leaf Placement
       is intentionally excluded — it's baked into the brp's OCC
       Location by FreeCAD's ``Feature::onChanged``.
    3. Outlier-filter on translation magnitude (1.5×IQR fence).
    4. Extract surviving brps, load via ``BRepTools.Read``, apply
       transform via ``BRepBuilderAPI_Transform``, accumulate into
       a ``TopoDS_Compound``.
    5. Tessellate + hand off to the same polydata + VTK path STEP uses.

    Outlier rejection
    -----------------
    Real FreeCAD assemblies often contain a handful of features
    whose Placement is wildly off — leftover construction history,
    broken Mirror/Pattern operations, parts the user "deleted" by
    moving them far away rather than removing them. These features
    are invisible in FreeCAD's own viewer (because the user worked
    around them) but visible to us, and they dominate the bounding
    box. The 1.5×IQR fence on translation magnitude is a standard
    box-plot outlier rule that catches genuine outliers (typically
    1-2 orders of magnitude past the median) without affecting
    normal assemblies.
    """
    if time.perf_counter() > deadline:
        raise TimeoutError("fcstd render: deadline exceeded before start")

    import os
    import tempfile
    import zipfile
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Trsf
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape

    from .parsers.cad_fcstd_parser import _parse_document_xml, _world_transform

    # Step 1: parse Document.xml from the FCStd zip.
    with zipfile.ZipFile(fcstd_path) as z:
        zip_names = set(z.namelist())
        if "Document.xml" not in zip_names:
            raise KeyError("fcstd render: Document.xml missing from archive")
        xml_raw = z.read("Document.xml")
    by_name = _parse_document_xml(xml_raw)
    if not by_name:
        raise KeyError("fcstd render: Document.xml empty or malformed")
    if time.perf_counter() > deadline:
        raise TimeoutError("fcstd render: Document.xml parse exceeded deadline")

    # Step 2: compose transforms. Map each requested brp to a feature
    # name (strip ``.Shape.brp``) and resolve via ``_world_transform``.
    # Members missing from by_name are dropped silently — usually
    # means the asset spec is stale vs the current file.
    enriched: list[dict] = []
    for member in members:
        brp = member.get("brp") if isinstance(member, dict) else None
        if not brp or brp not in zip_names:
            continue
        feature_name = brp[:-len(".Shape.brp")] if brp.endswith(".Shape.brp") else None
        if not feature_name or feature_name not in by_name:
            continue
        tform = _world_transform(by_name, feature_name)
        enriched.append({"brp": brp, "transform": list(tform)})

    if not enriched:
        raise KeyError("fcstd render: no members resolvable from current Document.xml")

    # Step 3: outlier filter.
    survivors = _filter_outlier_members(enriched)

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    loaded = 0

    # Step 4: extract + apply transforms. Letting BRepTools.Read pull
    # from disk is simpler than threading the zip bytes through OCP's
    # stream interface.
    with tempfile.TemporaryDirectory(prefix="fcstd-render-") as tmp:
        with zipfile.ZipFile(fcstd_path) as z:
            for i, member in enumerate(survivors):
                if time.perf_counter() > deadline:
                    raise TimeoutError(
                        f"fcstd render: loading brps exceeded deadline ({i}/{len(survivors)})"
                    )
                brp = member["brp"]
                target = os.path.join(tmp, f"{i}.brp")
                with z.open(brp) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                shape = TopoDS_Shape()
                ok = BRepTools.Read_s(shape, target, builder)
                if not ok or shape.IsNull():
                    continue
                tf = member["transform"]
                trsf = gp_Trsf()
                # OCC takes a 3x4 affine (rotation + translation);
                # FCStd placements are rigid, which OCC supports via
                # SetValues(...). The 4th row is implicitly (0,0,0,1).
                trsf.SetValues(
                    tf[0], tf[1], tf[2], tf[3],
                    tf[4], tf[5], tf[6], tf[7],
                    tf[8], tf[9], tf[10], tf[11],
                )
                transformed = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
                builder.Add(compound, transformed)
                loaded += 1

    if loaded == 0:
        raise KeyError("fcstd render: no brp members loaded")
    if time.perf_counter() > deadline:
        raise TimeoutError("fcstd render: brp accumulation exceeded deadline")

    BRepMesh_IncrementalMesh(compound, 0.5, False, 0.5, True)
    if time.perf_counter() > deadline:
        raise TimeoutError("fcstd render: tessellation exceeded deadline")

    poly = _compound_to_polydata(compound)
    if time.perf_counter() > deadline:
        raise TimeoutError("fcstd render: polydata build exceeded deadline")

    return _render_polydata(poly, projection=projection, size=size)


def _compound_to_polydata(compound):
    """Walk every face of ``compound``, collect triangle vertices +
    indices into numpy buffers, build a vtkPolyData in one pass.

    Faster than the iterative ``InsertNextPoint`` / ``InsertNextCell``
    pattern by orders of magnitude on big shapes, because each VTK
    call goes through Python attribute resolution + a C++ trampoline.
    """
    import numpy as np
    import vtk
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    points_list: list[np.ndarray] = []
    tris_list: list[np.ndarray] = []
    base = 0

    exp = TopExp_Explorer(compound, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            n_nodes = tri.NbNodes()
            pts = np.empty((n_nodes, 3), dtype=np.float32)
            for j in range(1, n_nodes + 1):
                p = tri.Node(j).Transformed(trsf)
                pts[j - 1, 0] = p.X()
                pts[j - 1, 1] = p.Y()
                pts[j - 1, 2] = p.Z()
            points_list.append(pts)

            n_tris = tri.NbTriangles()
            idx = np.empty((n_tris, 3), dtype=np.int64)
            for t in range(1, n_tris + 1):
                a, b, c = tri.Triangle(t).Get()
                idx[t - 1, 0] = base + a - 1
                idx[t - 1, 1] = base + b - 1
                idx[t - 1, 2] = base + c - 1
            tris_list.append(idx)
            base += n_nodes
        exp.Next()

    if not points_list:
        raise ValueError("compound has no triangulated faces")

    points = np.concatenate(points_list, axis=0)
    tris = np.concatenate(tris_list, axis=0)

    from vtk.util import numpy_support  # type: ignore

    vtk_points = vtk.vtkPoints()
    vtk_points.SetData(numpy_support.numpy_to_vtk(points, deep=True))

    # vtkCellArray needs a flat connectivity buffer of (3, a, b, c, 3, d, e, f, ...).
    flat = np.empty((tris.shape[0], 4), dtype=np.int64)
    flat[:, 0] = 3
    flat[:, 1:] = tris
    cells = vtk.vtkCellArray()
    cells.SetCells(
        tris.shape[0],
        numpy_support.numpy_to_vtkIdTypeArray(flat.reshape(-1), deep=True),
    )

    pd = vtk.vtkPolyData()
    pd.SetPoints(vtk_points)
    pd.SetPolys(cells)
    return pd


def _filter_outlier_members(members: list[dict]) -> list[dict]:
    """Drop members whose translation magnitude is past the 1.5×IQR
    fence — standard box-plot outlier rejection.

    Small assemblies (< ~10 members) skip filtering: too few samples
    to define IQR meaningfully. With more members we compute Q1, Q3,
    IQR, and drop anything beyond ``Q3 + 1.5×IQR``. Below-Q1 features
    can't be outliers in magnitude space (min is 0).

    The 1.5× fence is conservative — catches the genuine "150m off"
    placements we see from broken FreeCAD Mirror/Pattern history
    without touching normal spread.
    """
    import math

    if len(members) < 10:
        return members
    mags: list[tuple[float, dict]] = []
    for m in members:
        tf = m.get("transform")
        if not tf or len(tf) < 12:
            mags.append((0.0, m))
            continue
        tx, ty, tz = tf[3], tf[7], tf[11]
        mags.append((math.sqrt(tx * tx + ty * ty + tz * tz), m))
    sorted_mags = sorted(v for v, _ in mags)
    q1 = sorted_mags[len(sorted_mags) // 4]
    q3 = sorted_mags[(3 * len(sorted_mags)) // 4]
    fence = q3 + 1.5 * (q3 - q1)
    kept = [m for v, m in mags if v <= fence]
    dropped = len(members) - len(kept)
    if dropped:
        logger.info(
            "cad_render: dropped %d/%d outlier member(s) past 1.5×IQR "
            "translation-magnitude fence (q3=%.1f, fence=%.1f)",
            dropped, len(members), q3, fence,
        )
    return kept


def _robust_bbox_for_framing(polydata) -> tuple[float, ...] | None:
    """Return (xmin, xmax, ymin, ymax, zmin, zmax) using percentile
    clipping per axis. Robust against outlier vertices that would
    otherwise dominate the bbox.

    Falls back to None for empty / tiny meshes so the caller can use
    VTK's default ``ResetCamera`` instead.
    """
    import numpy as np
    from vtk.util import numpy_support  # type: ignore

    pts = polydata.GetPoints()
    if pts is None or pts.GetNumberOfPoints() < 10:
        return None
    arr = numpy_support.vtk_to_numpy(pts.GetData())
    if arr.shape[0] < 10:
        return None
    # 5th-95th percentile per axis. Cheap on a few million points.
    lo = np.percentile(arr, 5, axis=0)
    hi = np.percentile(arr, 95, axis=0)
    return (float(lo[0]), float(hi[0]),
            float(lo[1]), float(hi[1]),
            float(lo[2]), float(hi[2]))


def _view_aligned_extents(
    polydata, view_position: tuple[float, float, float], up: tuple[float, float, float],
) -> tuple[float, float] | None:
    """Project vertex cloud onto the camera image plane (right × up)
    and return the (u_extent, v_extent) 5-95 percentile spread.

    The plain world-AABB ``max(ext_x, ext_y, ext_z)`` heuristic
    over-estimates the on-screen size of tilted views: an L-shaped
    rig viewed iso (1,-1,1) has a much smaller silhouette than its
    longest world-axis extent. Projecting first gives the actual
    pixels-worth of model the camera will see, so framing is tight
    and consistent across front/top/side/iso.
    """
    import numpy as np
    from vtk.util import numpy_support  # type: ignore

    pts = polydata.GetPoints()
    if pts is None or pts.GetNumberOfPoints() < 10:
        return None
    arr = numpy_support.vtk_to_numpy(pts.GetData())
    if arr.shape[0] < 10:
        return None

    # Camera looks from ``view_position`` toward the focal point —
    # forward is the negated normalized position vector.
    forward = -np.asarray(view_position, dtype=np.float64)
    n = np.linalg.norm(forward)
    if n < 1e-9:
        return None
    forward /= n

    up_vec = np.asarray(up, dtype=np.float64)
    # Orthogonalize up against forward so right ⟂ up regardless of
    # whether the caller supplied a clean basis.
    up_vec = up_vec - forward * np.dot(up_vec, forward)
    un = np.linalg.norm(up_vec)
    if un < 1e-9:
        return None
    up_vec /= un
    right = np.cross(forward, up_vec)

    u = arr @ right
    v = arr @ up_vec
    u_lo, u_hi = np.percentile(u, [5, 95])
    v_lo, v_hi = np.percentile(v, [5, 95])
    return float(u_hi - u_lo), float(v_hi - v_lo)


_RENDER_BACKEND_LOGGED = False


def _log_render_backend_once(rw) -> None:
    """One-time per-process log of which VTK render-window backend
    actually came up. VTK's factory tries EGL (GPU) first and falls
    back to OSMesa (software) silently — we want operators to know
    which they're getting so a slow render isn't a mystery."""
    global _RENDER_BACKEND_LOGGED
    if _RENDER_BACKEND_LOGGED:
        return
    _RENDER_BACKEND_LOGGED = True
    cls = type(rw).__name__
    # Probe the actual OpenGL renderer string for clarity.
    try:
        ogl = rw.ReportCapabilities() or ""
        gl_renderer = ""
        for line in ogl.splitlines():
            if "OpenGL renderer" in line or "OpenGL vendor" in line:
                gl_renderer += line.strip() + "; "
    except Exception:
        gl_renderer = "<probe failed>"
    logger.info("CAD render backend: %s — %s", cls, gl_renderer or "<no caps>")


def _render_polydata(polydata, *, projection: str, size: int) -> bytes:
    """Render a vtkPolyData into a PNG byte string.

    Off-screen VTK pipeline: PolyDataNormals → PolyDataMapper →
    Actor → Renderer → vtkRenderWindow.SetOffScreenRendering(1) →
    vtkWindowToImageFilter → vtkPNGWriter to memory. No file IO.

    VTK's window factory tries hardware EGL first and falls back to
    software OSMesa silently. On a box where the running user is in
    the ``render`` group with NVIDIA drivers, the hardware path is
    automatic and renders are 50-100× faster. Without GPU access the
    software path still works — just slower. No code changes needed
    to switch; this function works either way.
    """
    import vtk

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(polydata)
    normals.SetSplitting(False)
    normals.SetConsistency(True)
    normals.Update()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(normals.GetOutputPort())

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(0.65, 0.70, 0.78)
    actor.GetProperty().SetAmbient(0.25)
    actor.GetProperty().SetDiffuse(0.75)
    actor.GetProperty().SetSpecular(0.10)

    ren = vtk.vtkRenderer()
    ren.SetBackground(1.0, 1.0, 1.0)
    ren.AddActor(actor)

    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.SetSize(size, size)
    rw.AddRenderer(ren)

    cam = ren.GetActiveCamera()
    cfg = _VIEWS[projection]
    cam.SetViewUp(*cfg["up"])
    if cfg["ortho"]:
        cam.SetParallelProjection(True)
    else:
        # Pin the perspective FOV so framing math is deterministic.
        # Default is 30° vertical; we keep that but want it explicit
        # so the distance math below matches.
        cam.SetViewAngle(30.0)

    # Compute a *robust* bounding box for camera framing — VTK's
    # default ``ResetCamera`` uses the actor's full geometric bbox,
    # which a single outlier mesh (e.g. a Spring placed at -150 m
    # from the rest of the assembly via FreeCAD's modeling history)
    # blows out by 40× and shrinks the visible model into a dot.
    # We instead frame the 5th-95th percentile of vertex positions
    # per axis: the bulk of the model fills the frame; outliers may
    # render at the edges. Same content, useful camera.
    bbox = _robust_bbox_for_framing(polydata)
    if bbox is None:
        ren.ResetCamera()
    else:
        import math

        # World-AABB centre — used as the focal point. The view-
        # aligned extent below sizes the frame; the focal point
        # just decides what's in the middle of it.
        cx = (bbox[0] + bbox[1]) / 2.0
        cy = (bbox[2] + bbox[3]) / 2.0
        cz = (bbox[4] + bbox[5]) / 2.0
        ext_x = bbox[1] - bbox[0]
        ext_y = bbox[3] - bbox[2]
        ext_z = bbox[5] - bbox[4]
        world_max_ext = max(ext_x, ext_y, ext_z, 1.0)

        # Normalize the view direction — the raw vectors in _VIEWS
        # are convenient triples like (1, -1, 1) for ISO, which have
        # magnitude > 1 and would otherwise push the camera too far.
        dx, dy, dz = cfg["position"]
        mag = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        dx, dy, dz = dx / mag, dy / mag, dz / mag

        # View-aligned extents (u along camera-right, v along
        # camera-up) — the actual on-screen size of the model in
        # this projection. Falls back to ``world_max_ext`` when the
        # mesh is too small for stable percentile stats.
        ext_uv = _view_aligned_extents(polydata, cfg["position"], cfg["up"])
        if ext_uv is None:
            screen_ext = world_max_ext
        else:
            screen_ext = max(ext_uv[0], ext_uv[1], 1.0)

        if cfg["ortho"]:
            # Orthographic — distance doesn't change scale, just
            # avoid clipping. Parallel scale = half the projected
            # extent divided by fill (90% frame fill = 5% padding
            # each side).
            fill = 0.90
            dist = world_max_ext * 2.0
            cam.SetFocalPoint(cx, cy, cz)
            cam.SetPosition(cx + dx * dist, cy + dy * dist, cz + dz * dist)
            cam.SetParallelScale((screen_ext / 2.0) / fill)
        else:
            # Perspective. Solve for the distance that puts the
            # projected ``screen_ext`` at ~88% of the vertical FOV.
            #
            #   visible_at_distance = 2 * dist * tan(yfov/2)
            #   we want visible = screen_ext / fill
            #
            # With yfov = 30°, fill = 0.88:
            #   dist = screen_ext / (0.88 * 2 * tan(15°))
            half_fov = math.radians(15.0)
            fill = 0.88
            dist = screen_ext / (fill * 2.0 * math.tan(half_fov))
            cam.SetFocalPoint(cx, cy, cz)
            cam.SetPosition(cx + dx * dist, cy + dy * dist, cz + dz * dist)
    ren.ResetCameraClippingRange()

    rw.Render()
    _log_render_backend_once(rw)

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rw)
    w2i.ReadFrontBufferOff()
    w2i.Update()

    writer = vtk.vtkPNGWriter()
    writer.WriteToMemoryOn()
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()
    blob = writer.GetResult()

    from vtk.util import numpy_support  # type: ignore

    arr = numpy_support.vtk_to_numpy(blob)
    return arr.tobytes()


# ---------------------------------------------------------------------------
# Boot-time registration
# ---------------------------------------------------------------------------


_HANDLER = CadProjectionHandler()
register(_HANDLER)
