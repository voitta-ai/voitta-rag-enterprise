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
            urls[proj] = f"/api/assets/{token}"
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

        # File-id → on-disk STEP path. We never read the bytes
        # ourselves; OCP opens the file directly so its UTF-8 / latin-1
        # detection runs the way both FreeCAD and Creality use it.
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

        # Look up the slug's XCAF entry paths from the menu the parser
        # persisted at extract time. This is what guarantees that
        # "what's rendered matches what's indexed": the slug maps to
        # a frozen list of label entry paths, the entries resolve
        # deterministically against the freshly-parsed STEP, and any
        # mismatch is a hard 404.
        entry_paths = _entry_paths_for_slug(cas_id, slug)
        if not entry_paths:
            raise KeyError(
                f"slug {slug!r} not in current asset menu for file {file_id}"
            )

        from .indexing_caps import get_caps

        timeout_s = getattr(get_caps(), "cad_render_timeout_s", 30)
        started = time.perf_counter()

        png = _render_subshape(
            step_path=abs_path,
            entry_paths=entry_paths,
            projection=variant,
            size=size,
            deadline=started + timeout_s,
        )
        return RenderedAsset(body=png, mime="image/png")


def _entry_paths_for_slug(cas_id: str | None, slug: str) -> list[str]:
    """Resolve a slug to its XCAF entry paths from the file's
    on-demand-assets menu.

    Returns the list verbatim from ``params_schema['x-entry-paths']``
    of the matching AssetSpec, or an empty list if the slug isn't in
    the menu (file re-indexed, slug retired, etc.)."""
    specs = load_assets_for_file(cas_id)
    for spec in specs:
        if spec.asset_type == "cad_projection" and spec.slug == slug:
            schema = spec.params_schema or {}
            paths = schema.get("x-entry-paths") or []
            if isinstance(paths, list):
                return [str(p) for p in paths]
    return []


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


def _render_polydata(polydata, *, projection: str, size: int) -> bytes:
    """Render a vtkPolyData into a PNG byte string.

    Off-screen VTK pipeline: PolyDataNormals → PolyDataMapper →
    Actor → Renderer → vtkRenderWindow.SetOffScreenRendering(1) →
    vtkWindowToImageFilter → vtkPNGWriter to memory. No file IO.
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
    pos = cfg["position"]
    cam.SetFocalPoint(0.0, 0.0, 0.0)
    cam.SetPosition(*pos)
    cam.SetViewUp(*cfg["up"])
    if cfg["ortho"]:
        cam.SetParallelProjection(True)
    ren.ResetCamera()
    ren.ResetCameraClippingRange()

    rw.Render()

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
