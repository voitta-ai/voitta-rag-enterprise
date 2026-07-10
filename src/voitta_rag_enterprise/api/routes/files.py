"""File detail + content endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...cas import store as cas_store
from ...db.models import Chunk, File, Folder, Image
from ...services.acl import CurrentUser, user_can_see_folder
from ..deps import current_user, db_session

router = APIRouter(prefix="/files", tags=["files"])


def _require_visible_file(db: Session, file_id: int, user: CurrentUser) -> File:
    """Load a file the caller is allowed to see, or 404.

    Authorization is folder-scoped: a file is reachable only when its
    folder is visible to ``user`` (owned, ACL-granted, community-shared,
    or single-user). This is the single seam every ``/files/{id}`` handler
    routes through — WITHOUT it, any authenticated principal could read or
    download any file in the deployment by enumerating integer ids
    (the folder-level routes enforce this; the file-level ones must too).

    404 (never 403) for both missing and not-visible so the existence of
    another account's file is not probeable — same rationale as
    ``folders._require_owner``.
    """
    file = db.get(File, file_id)
    if file is None or not user_can_see_folder(db, file.folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return file


class FileDetail(BaseModel):
    id: int
    folder_id: int
    rel_path: str
    state: str
    size_bytes: int | None
    mtime_ns: int | None
    last_indexed_at: int | None
    source_url: str | None
    tab: str | None
    file_cas_id: str | None
    pending_embeds: int


class FileImage(BaseModel):
    image_id: int
    image_index: int
    position: int | None
    page: int | None
    width: int | None
    height: int | None
    mime: str | None
    image_cas_id: str


def _to_file_detail(f: File) -> FileDetail:
    return FileDetail(
        id=f.id,
        folder_id=f.folder_id,
        rel_path=f.rel_path,
        state=f.state,
        size_bytes=f.size_bytes,
        mtime_ns=f.mtime_ns,
        last_indexed_at=f.last_indexed_at,
        source_url=f.source_url,
        tab=f.tab,
        file_cas_id=f.file_cas_id,
        pending_embeds=f.pending_embeds,
    )


@router.get("/{file_id}", response_model=FileDetail)
def get_file(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FileDetail:
    file = _require_visible_file(db, file_id, user)
    return _to_file_detail(file)


@router.get("/{file_id}/text", response_class=PlainTextResponse)
def get_file_text(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> str:
    file = _require_visible_file(db, file_id, user)
    if not file.file_cas_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "File not yet extracted")
    try:
        return cas_store.read_file_blob(file.file_cas_id, "text.md").decode("utf-8")
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extracted text missing") from e


@router.get("/{file_id}/raw")
def download_file(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> FileResponse:
    file = _require_visible_file(db, file_id, user)
    folder = db.get(Folder, file.folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    path = Path(folder.path) / file.rel_path
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not on disk")
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")


@router.get("/{file_id}/page-images", response_model=list[FileImage])
def get_file_page_images(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FileImage]:
    _require_visible_file(db, file_id, user)
    rows = (
        db.execute(
            select(Image)
            .where(Image.file_id == file_id, Image.kind == "page_render")
            .order_by(Image.page, Image.image_index)
        )
        .scalars()
        .all()
    )
    return [
        FileImage(
            image_id=img.id,
            image_index=img.image_index,
            position=None,
            page=img.page,
            width=img.width,
            height=img.height,
            mime=img.mime,
            image_cas_id=img.image_cas_id,
        )
        for img in rows
    ]


class EmailAttachment(BaseModel):
    filename: str
    content_type: str
    size: int


class EmailPreview(BaseModel):
    from_addr: str | None = None
    to: str | None = None
    cc: str | None = None
    bcc: str | None = None
    reply_to: str | None = None
    subject: str | None = None
    date: str | None = None
    message_id: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    attachments: list[EmailAttachment] = []


@router.get("/{file_id}/email", response_model=EmailPreview)
def get_file_email(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> EmailPreview:
    """Parse an RFC-822 .eml file into structured fields for preview."""
    import email
    from email import policy
    from email.utils import getaddresses

    file = _require_visible_file(db, file_id, user)
    if Path(file.rel_path).suffix.lower() != ".eml":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not an .eml file")
    folder = db.get(Folder, file.folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    path = Path(folder.path) / file.rel_path
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not on disk")

    # ``policy.default`` decodes headers + charset-aware body parts and
    # collapses multipart/alternative to a sensible ``get_body`` walk —
    # the manual ``walk()`` + decode dance compat.py used to need.
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)

    def _addr(header: str) -> str | None:
        raw = msg.get(header)
        if raw is None:
            return None
        # getaddresses unwraps "Name <addr>, addr2" groupings; rejoin
        # for display so the UI gets one tidy line per header.
        pairs = getaddresses([str(raw)])
        formatted = [
            f"{n} <{a}>" if n else a
            for n, a in pairs
            if a or n
        ]
        return ", ".join(formatted) or str(raw)

    body_html: str | None = None
    body_text: str | None = None
    try:
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            body_html = html_part.get_content()
    except Exception:
        body_html = None
    try:
        text_part = msg.get_body(preferencelist=("plain",))
        if text_part is not None:
            body_text = text_part.get_content()
    except Exception:
        body_text = None

    attachments: list[EmailAttachment] = []
    for part in msg.iter_attachments():
        fname = part.get_filename() or "(unnamed)"
        try:
            payload = part.get_payload(decode=True) or b""
            size = len(payload)
        except Exception:
            size = 0
        attachments.append(
            EmailAttachment(
                filename=fname,
                content_type=part.get_content_type(),
                size=size,
            )
        )

    return EmailPreview(
        from_addr=_addr("From"),
        to=_addr("To"),
        cc=_addr("Cc"),
        bcc=_addr("Bcc"),
        reply_to=_addr("Reply-To"),
        subject=str(msg.get("Subject")) if msg.get("Subject") else None,
        date=str(msg.get("Date")) if msg.get("Date") else None,
        message_id=str(msg.get("Message-ID")) if msg.get("Message-ID") else None,
        body_html=body_html,
        body_text=body_text,
        attachments=attachments,
    )


_CAD_EXTS = {".step", ".stp", ".iges", ".igs", ".fcstd"}


@router.get("/{file_id}/stl")
def get_file_stl(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> Response:
    """Convert a STEP/IGES/FCStd file to binary STL for the 3D preview."""
    file = _require_visible_file(db, file_id, user)
    ext = Path(file.rel_path).suffix.lower()
    if ext not in _CAD_EXTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot convert {ext!r} to STL")
    folder = db.get(Folder, file.folder_id)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    path = Path(folder.path) / file.rel_path
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not on disk")
    try:
        stl_bytes = _to_stl(str(path), ext)
    except ImportError:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "OCP not available")
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return Response(content=stl_bytes, media_type="model/stl")


def _to_stl(file_path: str, ext: str) -> bytes:
    if ext == ".fcstd":
        shape = _fcstd_to_compound(file_path)
    else:
        shape = _step_iges_to_shape(file_path, ext)
    return _shape_to_stl_bytes(shape)


def _step_iges_to_shape(file_path: str, ext: str):
    if ext in (".step", ".stp"):
        from OCP.STEPControl import STEPControl_Reader  # type: ignore[import]
        reader = STEPControl_Reader()
    else:
        from OCP.IGESControl import IGESControl_Reader  # type: ignore[import]
        reader = IGESControl_Reader()
    reader.ReadFile(file_path)
    reader.TransferRoots()
    return reader.OneShape()


def _fcstd_to_compound(file_path: str):
    """Walk a FreeCAD .FCStd archive, compose world transforms via the
    App::Part hierarchy, and accumulate every Part::Feature into a single
    TopoDS_Compound. Mirrors the geometry-assembly half of
    services.cad_render._render_fcstd_component (without the VTK render).
    """
    import os
    import tempfile
    import zipfile

    from OCP.BRep import BRep_Builder  # type: ignore[import]
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # type: ignore[import]
    from OCP.BRepTools import BRepTools  # type: ignore[import]
    from OCP.gp import gp_Trsf  # type: ignore[import]
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape  # type: ignore[import]

    from ...services.parsers.cad_fcstd_parser import (
        _parse_document_xml,
        _world_transform,
    )

    with zipfile.ZipFile(file_path) as z:
        zip_names = set(z.namelist())
        if "Document.xml" not in zip_names:
            raise ValueError("Document.xml missing from FCStd archive")
        xml_raw = z.read("Document.xml")
    by_name = _parse_document_xml(xml_raw)
    if not by_name:
        raise ValueError("Document.xml empty or malformed")

    # Every *.Shape.brp blob in the archive is a Part::Feature payload.
    brps = sorted(n for n in zip_names if n.endswith(".Shape.brp"))

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    loaded = 0

    with tempfile.TemporaryDirectory(prefix="fcstd-stl-") as tmp:
        with zipfile.ZipFile(file_path) as z:
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
                # OCC's SetValues takes the 3×4 affine; the implicit 4th
                # row is (0, 0, 0, 1). FCStd placements are rigid → safe.
                trsf.SetValues(
                    tform[0], tform[1], tform[2], tform[3],
                    tform[4], tform[5], tform[6], tform[7],
                    tform[8], tform[9], tform[10], tform[11],
                )
                transformed = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
                builder.Add(compound, transformed)
                loaded += 1

    if loaded == 0:
        raise ValueError("no renderable brp members in FCStd")
    return compound


def _shape_to_stl_bytes(shape) -> bytes:
    import os
    import tempfile

    from OCP.BRepMesh import BRepMesh_IncrementalMesh  # type: ignore[import]
    from OCP.StlAPI import StlAPI_Writer  # type: ignore[import]

    # Coarse tessellation tuned for the ~360px sidebar preview, not for
    # printable accuracy. ``isRelative=True`` makes ``linearDeflection``
    # a fraction of the bounding-box max dimension, so a 5 mm screw and
    # a 2 m machine frame both come out with sensible facet counts
    # without hand-tuning. The angular cap of 1.0 rad (~57°) lets the
    # mesher merge near-coplanar faces aggressively. ``inParallel=True``
    # uses all cores for the per-face work. Expect ~3-5× faster vs the
    # 0.1 mm absolute setting and proportionally smaller STL bytes.
    mesh = BRepMesh_IncrementalMesh(shape, 0.02, True, 1.0, True)
    mesh.Perform()

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        tmp = f.name
    try:
        writer = StlAPI_Writer()
        writer.Write(shape, tmp)
        return Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)


@router.get("/{file_id}/images", response_model=list[FileImage])
def get_file_images(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[FileImage]:
    _require_visible_file(db, file_id, user)
    # SPA renders these inline next to their anchor chunk; page renders
    # have no anchor and would just clutter the carousel. Page renders are
    # served separately via the MCP list_page_images / get_page_image tools.
    rows = (
        db.execute(
            select(Image)
            .where(Image.file_id == file_id, Image.kind == "figure")
            .order_by(Image.image_index)
        )
        .scalars()
        .all()
    )
    out: list[FileImage] = []
    for img in rows:
        # The anchor chunk's char_start is the splice point for inline rendering.
        # Standalone images have no anchor, so position stays None.
        position = None
        if img.anchor_chunk is not None:
            anchor = db.execute(
                select(Chunk).where(
                    Chunk.file_id == file_id, Chunk.chunk_index == img.anchor_chunk
                )
            ).scalar_one_or_none()
            position = anchor.char_start if anchor is not None else None
        out.append(
            FileImage(
                image_id=img.id,
                image_index=img.image_index,
                position=position,
                page=img.page,
                width=img.width,
                height=img.height,
                mime=img.mime,
                image_cas_id=img.image_cas_id,
            )
        )
    return out


class LayoutBlock(BaseModel):
    page: int
    type: str
    text: str | None = None


@router.get("/{file_id}/layout", response_model=list[LayoutBlock])
def get_file_layout(
    file_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> list[LayoutBlock]:
    import json

    file = _require_visible_file(db, file_id, user)
    if not file.file_cas_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "File not yet extracted")
    try:
        raw = cas_store.read_file_blob(file.file_cas_id, "page_layout.json")
    except FileNotFoundError:
        return []
    try:
        blocks = json.loads(raw)
    except Exception:
        return []
    out = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        page = blk.get("page")
        btype = blk.get("type") or "text"
        if not isinstance(page, int):
            continue
        text = blk.get("text") or None
        if isinstance(text, str):
            text = text[:120].strip() or None
        out.append(LayoutBlock(page=page, type=btype, text=text))
    return out
