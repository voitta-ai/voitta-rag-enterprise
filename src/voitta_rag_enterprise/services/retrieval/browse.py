"""Browse indexed storage: folder roots, directory listings, URL reverse lookup."""

from __future__ import annotations

from sqlalchemy import select

from ...db.database import session_scope
from ...db.models import File, Folder
from ..file_classify import source_kind as classify_source_kind
from .common import file_info
from .models import EntryInfo, FileInfo, FolderInfo
from .viewer import Viewer


def list_indexed_folders(
    viewer: Viewer, prefix: str | None = None
) -> list[FolderInfo] | list[EntryInfo]:
    """Roots listing (``prefix`` empty) or a directory listing under a folder.

    See the ``list_indexed_folders`` MCP tool for the full contract. A
    folder's ``active`` flag means "included in this viewer's searches"
    (``Viewer.scope``), so it is always true for ``scope="visible"``.
    """
    cleaned = (prefix or "").strip().strip("/")
    with session_scope() as s:
        visible_ids = viewer.visible_folder_ids(s)
        if visible_ids is None:
            visible_ids = set(s.execute(select(Folder.id)).scalars())
        active_ids = viewer.searchable_folder_ids(s)
        if active_ids is None:
            active_ids = visible_ids

        if not cleaned:
            out: list[FolderInfo] = []
            for f in s.execute(select(Folder).order_by(Folder.id)).scalars():
                if f.id not in visible_ids:
                    continue
                total = (
                    s.execute(
                        select(File).where(
                            File.folder_id == f.id, File.state != "deleted"
                        )
                    )
                    .scalars()
                    .all()
                )
                indexed = [x for x in total if x.state == "indexed"]
                out.append(
                    FolderInfo(
                        id=f.id,
                        path=f.path,
                        display_name=f.display_name,
                        source_type=f.source_type,
                        files_total=len(total),
                        files_indexed=len(indexed),
                        active=f.id in active_ids,
                        shared=bool(f.shared),
                    )
                )
            return out

        head, _, tail = cleaned.partition("/")
        # Resolve the top-level segment against visible folders by
        # display_name. Falls back to numeric id for unambiguous addressing
        # when two folders share a display_name.
        candidates = [
            f
            for f in s.execute(select(Folder).order_by(Folder.id)).scalars()
            if f.id in visible_ids and f.display_name == head
        ]
        if not candidates and head.isdigit():
            f = s.get(Folder, int(head))
            if f is not None and f.id in visible_ids:
                candidates = [f]
        if not candidates:
            return []
        folder = candidates[0]

        sub_prefix = tail  # rel-path within the folder; "" means root
        like_pat = f"{sub_prefix}/%" if sub_prefix else "%"

        rows = (
            s.execute(
                select(File).where(
                    File.folder_id == folder.id,
                    File.state != "deleted",
                    File.rel_path.like(like_pat),
                    ~File.rel_path.like("%.voitta.meta"),
                )
            )
            .scalars()
            .all()
        )

        dirs: dict[str, None] = {}
        files: list[File] = []
        depth = len(sub_prefix.split("/")) if sub_prefix else 0
        for r in rows:
            parts = r.rel_path.split("/")
            if sub_prefix and parts[:depth] != sub_prefix.split("/"):
                continue
            remainder = parts[depth:]
            if not remainder:
                continue
            head_seg = remainder[0]
            if head_seg.startswith("."):
                continue
            if len(remainder) == 1:
                if any(p.startswith(".") for p in parts):
                    continue
                files.append(r)
            else:
                if any(p.startswith(".") for p in parts[:depth + 1]):
                    continue
                dirs.setdefault(head_seg, None)

        base = f"{folder.display_name}/{sub_prefix}".rstrip("/")
        entries: list[EntryInfo] = []
        for name in sorted(dirs.keys()):
            entries.append(
                EntryInfo(
                    kind="folder",
                    name=name,
                    path=f"{base}/{name}/",
                    folder_id=folder.id,
                )
            )
        for f in sorted(files, key=lambda x: x.rel_path):
            name = f.rel_path.split("/")[-1]
            entries.append(
                EntryInfo(
                    kind="file",
                    name=name,
                    path=f"{base}/{name}",
                    folder_id=folder.id,
                    file_id=f.id,
                    state=f.state,
                    size_bytes=f.size_bytes,
                    source_url=f.source_url,
                    source_kind=classify_source_kind(f),
                )
            )
        return entries


def resolve_url(viewer: Viewer, url: str) -> list[FileInfo]:
    """Reverse-lookup an external URL (set by sync connectors) → matching files."""
    with session_scope() as s:
        rows = list(s.execute(select(File).where(File.source_url == url)).scalars())
        if not rows:
            # Fallback: prefix match (handles fragment-bearing URLs).
            rows = list(
                s.execute(
                    select(File).where(
                        File.source_url.is_not(None),
                        File.source_url == url.split("#", 1)[0],
                    )
                ).scalars()
            )
        # Folder-scope the results — a URL match must not reveal files in
        # folders the caller can't see (id/URL enumeration guard).
        return [file_info(f) for f in rows if viewer.can_see_folder(s, f.folder_id)]
