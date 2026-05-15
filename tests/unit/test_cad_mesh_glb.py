"""Unit tests for the GLB writer in services.cad_mesh.

The OCP-dependent compose / tessellate paths are exercised in
integration tests against fixture STEP / FCStd files. Here we only
verify the pure-numpy byte emitter — assemble a hand-rolled tetrahedron
(no OCP required) and confirm the resulting GLB is well-formed.
"""

from __future__ import annotations

import json
import struct

import numpy as np

from voitta_rag_enterprise.services.cad_mesh import (
    _arrays_to_glb,
    _vertex_normals,
)


def _unit_tetrahedron() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """4 vertices, 4 triangular faces. The simplest valid mesh."""
    positions = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )
    indices = np.array(
        [
            0, 1, 2,
            0, 1, 3,
            0, 2, 3,
            1, 2, 3,
        ],
        dtype=np.uint32,
    )
    normals = _vertex_normals(positions, indices)
    return positions, normals, indices


def _parse_glb(blob: bytes) -> tuple[dict, bytes]:
    """Crack a GLB into (json_header, bin_payload). Raises on bad
    magic / length mismatches."""
    magic, version, total_len = struct.unpack_from("<III", blob, 0)
    assert magic == 0x46546C67, f"bad magic: {magic:#x}"
    assert version == 2, f"unexpected glTF version {version}"
    assert total_len == len(blob), f"length mismatch {total_len} vs {len(blob)}"

    off = 12
    json_len, json_kind = struct.unpack_from("<II", blob, off)
    assert json_kind == 0x4E4F534A, f"bad JSON chunk kind {json_kind:#x}"
    off += 8
    json_str = blob[off : off + json_len].rstrip(b" ").decode("utf-8")
    gltf = json.loads(json_str)
    off += json_len

    bin_len, bin_kind = struct.unpack_from("<II", blob, off)
    assert bin_kind == 0x004E4942, f"bad BIN chunk kind {bin_kind:#x}"
    off += 8
    bin_payload = blob[off : off + bin_len]
    assert len(bin_payload) == bin_len
    return gltf, bin_payload


def test_single_part_glb_well_formed() -> None:
    pos, nrm, idx = _unit_tetrahedron()
    blob = _arrays_to_glb([("tet", pos, nrm, idx)])

    gltf, bin_payload = _parse_glb(blob)

    # Basic structure
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["scene"] == 0
    assert gltf["scenes"] == [{"nodes": [0]}]
    assert gltf["nodes"] == [{"name": "tet", "mesh": 0}]

    # One mesh, one primitive, POSITION + NORMAL + indices
    assert len(gltf["meshes"]) == 1
    prim = gltf["meshes"][0]["primitives"][0]
    assert set(prim["attributes"]) == {"POSITION", "NORMAL"}
    assert prim["mode"] == 4  # TRIANGLES

    # Three accessors (positions, normals, indices)
    assert len(gltf["accessors"]) == 3
    pos_acc = gltf["accessors"][prim["attributes"]["POSITION"]]
    assert pos_acc["count"] == 4
    assert pos_acc["type"] == "VEC3"
    assert pos_acc["min"] == [0.0, 0.0, 0.0]
    assert pos_acc["max"] == [1.0, 1.0, 1.0]

    idx_acc = gltf["accessors"][prim["indices"]]
    assert idx_acc["count"] == 12
    assert idx_acc["type"] == "SCALAR"
    assert idx_acc["componentType"] == 5125  # UNSIGNED_INT

    # Binary buffer holds exactly the bytes our views point at.
    assert len(bin_payload) == gltf["buffers"][0]["byteLength"]


def test_multi_part_preserves_names_and_isolates_buffers() -> None:
    pos1, nrm1, idx1 = _unit_tetrahedron()
    pos2, nrm2, idx2 = _unit_tetrahedron()
    pos2 = pos2 + np.array([10.0, 0.0, 0.0], dtype=np.float32)
    blob = _arrays_to_glb([("alpha", pos1, nrm1, idx1), ("beta", pos2, nrm2, idx2)])

    gltf, _bin = _parse_glb(blob)
    assert [n["name"] for n in gltf["nodes"]] == ["alpha", "beta"]
    assert [m["name"] for m in gltf["meshes"]] == ["alpha", "beta"]
    # Each part contributes 3 accessors (pos / normal / indices)
    assert len(gltf["accessors"]) == 6
    # bufferView byteOffsets must be strictly increasing and 4-byte aligned.
    offsets = [bv["byteOffset"] for bv in gltf["bufferViews"]]
    assert offsets == sorted(offsets)
    for o in offsets:
        assert o % 4 == 0


def test_normals_unit_length_and_no_nan() -> None:
    pos, _nrm, idx = _unit_tetrahedron()
    normals = _vertex_normals(pos, idx)
    assert normals.shape == pos.shape
    assert not np.isnan(normals).any()
    lens = np.linalg.norm(normals, axis=1)
    # Every non-degenerate vertex normal should be unit length.
    assert np.all(np.abs(lens - 1.0) < 1e-5)


def test_empty_parts_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        _arrays_to_glb([])
