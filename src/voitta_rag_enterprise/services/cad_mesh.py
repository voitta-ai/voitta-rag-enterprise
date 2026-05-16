"""On-demand 3D mesh export (glTF binary / GLB) for CAD files.

Registers ``cad_mesh`` with the asset-handler framework. The MCP
``request_asset`` call mints one signed URL; the HTTP fetch path lands
here and returns a binary GLB suitable for three.js ``GLTFLoader``.

Pipeline per fetch:

1. Compose the geometry as a ``list[(name, TopoDS_Shape)]``:
   * **No slug, FCStd** — walk every ``*.Shape.brp`` in the archive,
     resolve world transforms from ``Document.xml``, apply them.
   * **No slug, STEP/IGES** — read the file via OCP's one-shot reader,
     emit one node named after the file.
   * **Slug, FCStd** — look up the parser-emitted spec for the slug,
     load only the listed ``x-fcstd-members`` (reuses the same on-disk
     menu ``cad_projection`` consumes).
   * **Slug, STEP** — same idea via ``x-entry-paths``: resolve each
     XCAF entry to a labelled shape and emit one node per entry.
2. Tessellate each part via ``BRepMesh_IncrementalMesh``.
3. Walk each part's faces, pack positions / indices into numpy arrays,
   compute area-weighted vertex normals.
4. Emit a glTF 2.0 scene with one node per part (so three.js can hide /
   colour / click-select components by ``node.name``).
5. Wrap the JSON + binary buffer in the GLB chunk format.

Why GLB rather than STL
-----------------------
The pre-existing ``GET /files/{id}/stl`` endpoint flattens the whole
assembly into a single mono-mesh — no part hierarchy, no names. That's
fine for the SPA's preview sidebar but useless for any caller that
wants to inspect individual components. GLB preserves the structure
the CAD parser already extracted, and three.js loads it natively.

Why not lift code from ``api/routes/files.py``
----------------------------------------------
The STL endpoint already works and is exercised by the SPA every time
a user opens a CAD file. The compose primitives here are near-copies
of ``_fcstd_to_compound`` / ``_render_fcstd_component`` /
``_render_subshape`` — but with two changes (yield per-part shapes
instead of a single compound, skip the VTK render step). Duplicating
~30 lines is cheaper than threading a "compound vs named-list" mode
through both call sites.
"""

from __future__ import annotations

import json
import logging
import struct
from pathlib import Path
from typing import Any

import numpy as np

from .asset_handlers import (
    AssetHandler,
    AssetResponse,
    AssetSpec,
    RenderedAsset,
    load_assets_for_file,
    register,
)
from .signed_assets import issue_token

logger = logging.getLogger(__name__)

ASSET_TYPE = "cad_mesh"
_CAD_EXTS = {".step", ".stp", ".iges", ".igs", ".fcstd"}

# Tessellation defaults — same family as the existing render / STL code
# paths. ``linear_deflection`` is in millimetres when ``isRelative=False``
# (OCP convention). 0.5 mm is the cad_render setting; the STL path uses
# 0.02 relative which is roughly equivalent on typical assemblies.
_DEFAULT_LINEAR_DEFLECTION = 0.5
_DEFAULT_ANGULAR_DEFLECTION = 0.5  # radians

# GLB constants (glTF 2.0 binary spec).
_GLB_MAGIC = 0x46546C67   # 'glTF'
_GLB_VERSION = 2
_GLB_JSON_CHUNK = 0x4E4F534A   # 'JSON'
_GLB_BIN_CHUNK = 0x004E4942    # 'BIN\0'
_GLTF_FLOAT = 5126
_GLTF_UNSIGNED_INT = 5125
_GLTF_ARRAY_BUFFER = 34962
_GLTF_ELEMENT_ARRAY_BUFFER = 34963
_GLTF_TRIANGLES = 4


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CadMeshHandler(AssetHandler):
    """glTF-binary mesh export for CAD files.

    Two modes selected by ``slug``:

    * ``slug=None`` → export the entire file.
    * ``slug=<component>`` → export just that component. Slugs match
      the ``cad_projection`` slugs the parser writes at index time, so
      the LLM can reuse them across asset types.
    """

    asset_type = ASSET_TYPE

    def params_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "linear_deflection": {
                    "type": "number",
                    "minimum": 0.001,
                    "maximum": 5.0,
                    "default": _DEFAULT_LINEAR_DEFLECTION,
                    "description": (
                        "Tessellation tolerance in millimetres. Smaller "
                        "→ more triangles, smoother surface, larger GLB. "
                        "0.5 is good for previews; 0.05 for inspection."
                    ),
                },
            },
            "additionalProperties": False,
        }

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("linear_deflection", _DEFAULT_LINEAR_DEFLECTION)
        try:
            v = float(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"linear_deflection: expected number, got {raw!r}"
            ) from e
        if not (0.001 <= v <= 5.0):
            raise ValueError(
                f"linear_deflection: {v} out of range [0.001, 5.0]"
            )
        return {"linear_deflection": v}

    def request(
        self,
        *,
        file_id: int,
        slug: str | None,
        params: dict[str, Any],
        user_id: int | None,
    ) -> AssetResponse:
        from ..config import get_settings
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
            ext = Path(f.rel_path).suffix.lower()

        if ext not in _CAD_EXTS:
            raise ValueError(
                f"cad_mesh: unsupported extension {ext!r}; expected one of "
                f"{sorted(_CAD_EXTS)}"
            )
        if not abs_path.is_file():
            raise FileNotFoundError(
                f"cad_mesh: source bytes for file {file_id} not on disk "
                f"at {abs_path}"
            )

        settings = get_settings()
        token, expires_at = issue_token(
            file_id=file_id,
            asset_type=self.asset_type,
            slug=slug,
            params={**params, "__variant__": "mesh"},
            user_id=user_id,
        )
        return AssetResponse(
            asset_type=self.asset_type,
            urls={"mesh": settings.asset_url(token)},
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
            abs_path = str(Path(folder.path) / f.rel_path)
            rel_path = f.rel_path
            ext = Path(rel_path).suffix.lower()
            cas_id = f.file_cas_id

        deflection = float(
            params.get("linear_deflection", _DEFAULT_LINEAR_DEFLECTION)
        )

        if slug:
            spec_schema = _spec_schema_for_slug(cas_id, slug)
            if spec_schema is None:
                raise KeyError(
                    f"cad_mesh: slug {slug!r} not in asset menu for "
                    f"file {file_id}"
                )
            if "x-fcstd-members" in spec_schema:
                parts = _compose_fcstd_components(
                    abs_path, spec_schema["x-fcstd-members"]
                )
            elif "x-entry-paths" in spec_schema:
                parts = _compose_step_subshape(
                    abs_path,
                    [str(e) for e in spec_schema["x-entry-paths"]],
                )
            else:
                raise KeyError(
                    f"cad_mesh: slug {slug!r} has no recognized render hint "
                    f"(neither x-fcstd-members nor x-entry-paths)"
                )
        else:
            if ext == ".fcstd":
                parts = _compose_fcstd_all(abs_path)
            elif ext in (".step", ".stp", ".iges", ".igs"):
                parts = _compose_step_iges_all(abs_path, ext, rel_path)
            else:
                raise ValueError(
                    f"cad_mesh: unsupported extension {ext!r}"
                )

        named = _tessellate_parts(parts, linear_deflection=deflection)
        if not named:
            raise ValueError(
                "cad_mesh: no triangulated geometry produced"
            )
        glb = _arrays_to_glb(named)
        return RenderedAsset(body=glb, mime="model/gltf-binary")


def _spec_schema_for_slug(cas_id: str | None, slug: str) -> dict | None:
    """Reuse ``cad_projection`` (or any other) per-slug spec for this
    file. Slugs are unique per file across asset types, so a plain
    match-by-slug is sufficient."""
    for spec in load_assets_for_file(cas_id):
        if spec.slug == slug and spec.params_schema:
            return spec.params_schema
    return None


# ---------------------------------------------------------------------------
# Geometry composition — near-copies of cad_render / files.py paths but
# yielding (name, shape) tuples instead of a single TopoDS_Compound.
# ---------------------------------------------------------------------------


def _compose_fcstd_all(fcstd_path: str) -> list[tuple[str, Any]]:
    """Mirror of ``api.routes.files._fcstd_to_compound`` but emit a
    list of ``(feature_name, transformed_shape)`` instead of a single
    compound."""
    import os
    import tempfile
    import zipfile

    from OCP.BRep import BRep_Builder  # type: ignore[import]
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # type: ignore[import]
    from OCP.BRepTools import BRepTools  # type: ignore[import]
    from OCP.gp import gp_Trsf  # type: ignore[import]
    from OCP.TopoDS import TopoDS_Shape  # type: ignore[import]

    from .parsers.cad_fcstd_parser import (
        _parse_document_xml,
        _world_transform,
    )

    with zipfile.ZipFile(fcstd_path) as z:
        zip_names = set(z.namelist())
        if "Document.xml" not in zip_names:
            raise ValueError("Document.xml missing from FCStd archive")
        xml_raw = z.read("Document.xml")
    by_name = _parse_document_xml(xml_raw)
    if not by_name:
        raise ValueError("Document.xml empty or malformed")

    brps = sorted(n for n in zip_names if n.endswith(".Shape.brp"))
    parts: list[tuple[str, Any]] = []
    builder = BRep_Builder()
    with tempfile.TemporaryDirectory(prefix="fcstd-mesh-") as tmp:
        with zipfile.ZipFile(fcstd_path) as z:
            for i, brp in enumerate(brps):
                feature_name = brp[: -len(".Shape.brp")]
                if feature_name not in by_name:
                    continue
                tform = _world_transform(by_name, feature_name)
                target = os.path.join(tmp, f"{i}.brp")
                with z.open(brp) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                shape = TopoDS_Shape()
                if not BRepTools.Read_s(shape, target, builder) or shape.IsNull():
                    continue
                trsf = gp_Trsf()
                trsf.SetValues(
                    tform[0], tform[1], tform[2], tform[3],
                    tform[4], tform[5], tform[6], tform[7],
                    tform[8], tform[9], tform[10], tform[11],
                )
                transformed = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
                # Human label path matches what list_assets surfaces;
                # internal name kept as a unique disambiguator suffix.
                display = _label_path(by_name, feature_name)
                node_name = (
                    f"{display} [{feature_name}]"
                    if display and display != feature_name
                    else feature_name
                )
                parts.append((node_name, transformed))
    if not parts:
        raise ValueError("no renderable brp members in FCStd")
    return parts


def _compose_fcstd_components(
    fcstd_path: str, members: list[dict]
) -> list[tuple[str, Any]]:
    """Mirror of ``cad_render._render_fcstd_component`` up to the
    compound assembly step — but emit per-part shapes for the glTF
    writer instead of feeding a tessellator+VTK."""
    import os
    import tempfile
    import zipfile

    from OCP.BRep import BRep_Builder  # type: ignore[import]
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # type: ignore[import]
    from OCP.BRepTools import BRepTools  # type: ignore[import]
    from OCP.gp import gp_Trsf  # type: ignore[import]
    from OCP.TopoDS import TopoDS_Shape  # type: ignore[import]

    from .parsers.cad_fcstd_parser import (
        _parse_document_xml,
        _world_transform,
    )

    with zipfile.ZipFile(fcstd_path) as z:
        zip_names = set(z.namelist())
        if "Document.xml" not in zip_names:
            raise KeyError("Document.xml missing from FCStd archive")
        xml_raw = z.read("Document.xml")
    by_name = _parse_document_xml(xml_raw)
    if not by_name:
        raise KeyError("Document.xml empty or malformed")

    builder = BRep_Builder()
    parts: list[tuple[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="fcstd-mesh-") as tmp:
        with zipfile.ZipFile(fcstd_path) as z:
            for i, member in enumerate(members):
                brp = member.get("brp") if isinstance(member, dict) else None
                if not brp or brp not in zip_names:
                    continue
                feature_name = (
                    brp[: -len(".Shape.brp")]
                    if brp.endswith(".Shape.brp")
                    else brp
                )
                if not feature_name or feature_name not in by_name:
                    continue
                tform = _world_transform(by_name, feature_name)
                target = os.path.join(tmp, f"{i}.brp")
                with z.open(brp) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                shape = TopoDS_Shape()
                if not BRepTools.Read_s(shape, target, builder) or shape.IsNull():
                    continue
                trsf = gp_Trsf()
                trsf.SetValues(
                    tform[0], tform[1], tform[2], tform[3],
                    tform[4], tform[5], tform[6], tform[7],
                    tform[8], tform[9], tform[10], tform[11],
                )
                transformed = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
                display = _label_path(by_name, feature_name)
                node_name = (
                    f"{display} [{feature_name}]"
                    if display and display != feature_name
                    else feature_name
                )
                parts.append((node_name, transformed))
    if not parts:
        raise KeyError(
            "cad_mesh: no members resolvable from current Document.xml"
        )
    return parts


def _compose_step_iges_all(
    file_path: str, ext: str, rel_path: str
) -> list[tuple[str, Any]]:
    """One-shot read of a STEP/IGES file. The reader returns a single
    ``TopoDS_Shape`` (typically a compound for assemblies) — we emit
    one node named after the source file."""
    if ext in (".step", ".stp"):
        from OCP.STEPControl import STEPControl_Reader  # type: ignore[import]
        reader = STEPControl_Reader()
    else:
        from OCP.IGESControl import IGESControl_Reader  # type: ignore[import]
        reader = IGESControl_Reader()
    reader.ReadFile(file_path)
    reader.TransferRoots()
    return [(Path(rel_path).stem or "model", reader.OneShape())]


def _compose_step_subshape(
    step_path: str, entry_paths: list[str]
) -> list[tuple[str, Any]]:
    """Mirror of ``cad_render._render_subshape`` shape-resolution step.
    Returns one (entry_name, shape) per resolved XCAF label."""
    from OCP.TCollection import TCollection_AsciiString  # type: ignore[import]
    from OCP.TDF import TDF_Label, TDF_Tool  # type: ignore[import]
    from OCP.XCAFDoc import XCAFDoc_DocumentTool  # type: ignore[import]

    from .parsers.cad_step_parser import _parse_step_into_xcaf

    doc, _schema = _parse_step_into_xcaf(Path(step_path))
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    data = doc.GetData()

    parts: list[tuple[str, Any]] = []
    for entry in entry_paths:
        label = TDF_Label()
        TDF_Tool.Label_s(data, TCollection_AsciiString(entry), label, False)
        if label.IsNull():
            continue
        shape = shape_tool.GetShape_s(label)
        if shape.IsNull():
            continue
        parts.append((entry, shape))
    if not parts:
        raise KeyError(
            "cad_mesh: none of the entry paths resolved to geometry — "
            "STEP may have been re-uploaded since the menu was built"
        )
    return parts


# ---------------------------------------------------------------------------
# Tessellation — TopoDS_Shape → numpy arrays.
# ---------------------------------------------------------------------------


def _label_path(by_name: dict, feature_name: str) -> str:
    """Build a human-readable label path by walking the parent chain in
    ``by_name`` (``{name → _Obj}`` from ``_parse_document_xml``). Falls
    back to the internal name if no label is set. Example:
    ``"4 post lift / base frame / Longitudinal rail L"``."""
    segments: list[str] = []
    cursor = by_name.get(feature_name)
    seen: set[str] = set()
    while cursor is not None and cursor.name not in seen:
        seen.add(cursor.name)
        segments.append((cursor.label or cursor.name).strip() or cursor.name)
        cursor = by_name.get(cursor.parent) if cursor.parent else None
    return " / ".join(reversed(segments)) if segments else feature_name


def _tessellate_parts(
    parts: list[tuple[str, Any]],
    *,
    linear_deflection: float,
) -> list[tuple[str, np.ndarray, np.ndarray, np.ndarray]]:
    """Tessellate each shape and pack into ``(name, positions, normals,
    indices)`` tuples ready for the GLB writer.

    Empty parts (no triangulated faces — e.g. a sketch-only feature)
    are silently dropped. Normals are area-weighted vertex averages,
    which gives a smooth look without needing OCP per-vertex normal
    storage."""
    out: list[tuple[str, np.ndarray, np.ndarray, np.ndarray]] = []
    for name, shape in parts:
        arrays = _shape_to_arrays(
            shape, linear_deflection=linear_deflection
        )
        if arrays is None:
            continue
        positions, indices = arrays
        normals = _vertex_normals(positions, indices)
        out.append((name, positions, normals, indices))
    return out


def _shape_to_arrays(
    shape: Any, *, linear_deflection: float
) -> tuple[np.ndarray, np.ndarray] | None:
    """Tessellate a single ``TopoDS_Shape``; return packed positions +
    flat index buffer (``uint32``). ``None`` if no triangulation."""
    from OCP.BRep import BRep_Tool  # type: ignore[import]
    from OCP.BRepMesh import BRepMesh_IncrementalMesh  # type: ignore[import]
    from OCP.TopAbs import TopAbs_FACE  # type: ignore[import]
    from OCP.TopExp import TopExp_Explorer  # type: ignore[import]
    from OCP.TopLoc import TopLoc_Location  # type: ignore[import]
    from OCP.TopoDS import TopoDS  # type: ignore[import]

    BRepMesh_IncrementalMesh(
        shape,
        linear_deflection,
        False,
        _DEFAULT_ANGULAR_DEFLECTION,
        True,
    )

    points_chunks: list[np.ndarray] = []
    tris_chunks: list[np.ndarray] = []
    base = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
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
            points_chunks.append(pts)
            n_tris = tri.NbTriangles()
            idx = np.empty((n_tris, 3), dtype=np.uint32)
            for t in range(1, n_tris + 1):
                a, b, c = tri.Triangle(t).Get()
                idx[t - 1, 0] = base + a - 1
                idx[t - 1, 1] = base + b - 1
                idx[t - 1, 2] = base + c - 1
            tris_chunks.append(idx)
            base += n_nodes
        exp.Next()
    if not points_chunks:
        return None
    positions = np.concatenate(points_chunks, axis=0)
    indices = np.concatenate(tris_chunks, axis=0).reshape(-1)
    return positions, indices


def _vertex_normals(
    positions: np.ndarray, indices: np.ndarray
) -> np.ndarray:
    """Area-weighted per-vertex normals. ``positions`` is (N, 3) float32;
    ``indices`` is flat uint32 of length 3*T. The cross product is
    *not* normalized per-triangle — its magnitude is proportional to
    triangle area, which is exactly the weight we want before summing
    into the vertex bins."""
    tris = indices.reshape(-1, 3).astype(np.int64)
    v0 = positions[tris[:, 0]]
    v1 = positions[tris[:, 1]]
    v2 = positions[tris[:, 2]]
    face_normals = np.cross(v1 - v0, v2 - v0)
    normals = np.zeros_like(positions)
    np.add.at(normals, tris[:, 0], face_normals)
    np.add.at(normals, tris[:, 1], face_normals)
    np.add.at(normals, tris[:, 2], face_normals)
    norm = np.linalg.norm(normals, axis=1, keepdims=True)
    safe = np.where(norm > 1e-12, normals / np.where(norm > 0, norm, 1), 0.0)
    # Replace any all-zero rows (isolated vertex / degenerate) with +Z
    # so the GLB stays valid; three.js would render NaN normals as
    # invisible faces otherwise.
    zero_rows = (norm.reshape(-1) <= 1e-12)
    if zero_rows.any():
        safe[zero_rows] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    return safe.astype(np.float32)


# ---------------------------------------------------------------------------
# GLB writer. Pure Python + numpy; no third-party glTF library.
# ---------------------------------------------------------------------------


def _pad4(buf: bytes, fill: bytes = b"\x00") -> bytes:
    pad = (4 - len(buf) % 4) % 4
    return buf + fill * pad


def _arrays_to_glb(
    named_parts: list[tuple[str, np.ndarray, np.ndarray, np.ndarray]],
) -> bytes:
    """Pack ``[(name, positions, normals, indices), ...]`` into a GLB.

    Each part becomes one glTF node referencing a mesh with a single
    primitive (POSITION + NORMAL attributes, indexed TRIANGLES). All
    accessors share one binary buffer.

    Indices are unconditionally ``UNSIGNED_INT`` (uint32). The glTF
    spec also allows uint16 / uint8 but the size win on engineering
    meshes is small (most parts exceed 65k vertices in aggregate even
    if individual parts don't) and choosing per-part adds branching
    for no real benefit.
    """
    if not named_parts:
        raise ValueError("cad_mesh: cannot write empty GLB")

    buffer_views: list[dict] = []
    accessors: list[dict] = []
    meshes: list[dict] = []
    nodes: list[dict] = []
    bin_chunks: list[bytes] = []
    offset = 0

    def _add_view(data: bytes, target: int) -> int:
        nonlocal offset
        bin_chunks.append(_pad4(data))
        bv_idx = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(data),
            "target": target,
        })
        offset += len(_pad4(data))
        return bv_idx

    for name, positions, normals, indices in named_parts:
        if positions.dtype != np.float32:
            positions = positions.astype(np.float32)
        if normals.dtype != np.float32:
            normals = normals.astype(np.float32)
        if indices.dtype != np.uint32:
            indices = indices.astype(np.uint32)

        pmin = [float(positions[:, 0].min()), float(positions[:, 1].min()), float(positions[:, 2].min())]
        pmax = [float(positions[:, 0].max()), float(positions[:, 1].max()), float(positions[:, 2].max())]

        pos_bv = _add_view(positions.tobytes(), _GLTF_ARRAY_BUFFER)
        accessors.append({
            "bufferView": pos_bv,
            "componentType": _GLTF_FLOAT,
            "count": int(positions.shape[0]),
            "type": "VEC3",
            "min": pmin,
            "max": pmax,
        })
        pos_acc = len(accessors) - 1

        nrm_bv = _add_view(normals.tobytes(), _GLTF_ARRAY_BUFFER)
        accessors.append({
            "bufferView": nrm_bv,
            "componentType": _GLTF_FLOAT,
            "count": int(normals.shape[0]),
            "type": "VEC3",
        })
        nrm_acc = len(accessors) - 1

        idx_bv = _add_view(indices.tobytes(), _GLTF_ELEMENT_ARRAY_BUFFER)
        accessors.append({
            "bufferView": idx_bv,
            "componentType": _GLTF_UNSIGNED_INT,
            "count": int(indices.shape[0]),
            "type": "SCALAR",
        })
        idx_acc = len(accessors) - 1

        mesh_idx = len(meshes)
        meshes.append({
            "name": name,
            "primitives": [{
                "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
                "indices": idx_acc,
                "mode": _GLTF_TRIANGLES,
            }],
        })
        nodes.append({"name": name, "mesh": mesh_idx})

    bin_buf = b"".join(bin_chunks)

    gltf = {
        "asset": {"version": "2.0", "generator": "voitta-rag-enterprise/cad_mesh"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "buffers": [{"byteLength": len(bin_buf)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }

    json_chunk = _pad4(
        json.dumps(gltf, separators=(",", ":")).encode("utf-8"),
        fill=b" ",
    )
    total_len = 12 + 8 + len(json_chunk) + 8 + len(bin_buf)
    return (
        struct.pack("<III", _GLB_MAGIC, _GLB_VERSION, total_len)
        + struct.pack("<II", len(json_chunk), _GLB_JSON_CHUNK)
        + json_chunk
        + struct.pack("<II", len(bin_buf), _GLB_BIN_CHUNK)
        + bin_buf
    )


# ---------------------------------------------------------------------------
# Boot-time registration + list_assets entry.
# ---------------------------------------------------------------------------


_HANDLER = CadMeshHandler()
register(_HANDLER)


def spec_for(file_id: int, rel_path: str | None) -> AssetSpec:
    """Build the whole-file ``list_assets`` menu entry. Only emitted by
    ``mcp_server.list_assets`` when the file extension is in
    :data:`_CAD_EXTS`."""
    name = Path(rel_path).name if rel_path else f"file {file_id}"
    return AssetSpec(
        asset_type=ASSET_TYPE,
        label=f"3D mesh / glTF binary ({name})",
        description=(
            "Return a binary glTF (.glb) of the CAD model via a signed "
            "URL. Mime is model/gltf-binary. The scene contains one "
            "node per component (FCStd: one per Part::Feature; STEP: "
            "one per top-level XCAF label), so a viewer (three.js "
            "GLTFLoader, Babylon.js, Blender) can list / hide / colour "
            "parts by name. Without ``slug`` the whole file is "
            "exported; pass a slug from ``cad_projection`` to export "
            "just that component."
        ),
        slug=None,
        params_schema={
            "type": "object",
            "properties": {
                "linear_deflection": {
                    "type": "number",
                    "minimum": 0.001,
                    "maximum": 5.0,
                    "default": _DEFAULT_LINEAR_DEFLECTION,
                },
            },
            "additionalProperties": False,
        },
        examples=(
            {"asset_type": ASSET_TYPE, "slug": None, "params": {}},
        ),
    )


def is_cad_file(rel_path: str | None) -> bool:
    """Used by ``list_assets`` to decide whether to surface ``cad_mesh``."""
    if not rel_path:
        return False
    return Path(rel_path).suffix.lower() in _CAD_EXTS
