"""Folders whose content is indexed IN PLACE — read-only, never app-managed.

Two ``source_type`` values share this contract:

* ``google_drive_local`` — the user's Google Drive for Desktop mount.
* ``local_link`` — a directory on the host, below the admin-set
  linked-folder root (``admin_store.get_link_root``).

For both, ``folder.path`` points OUTSIDE ``VOITTA_ROOT_PATH`` at a tree
someone else owns. Every rule that follows from that lives behind this one
predicate, so it is impossible to apply half of them:

* the scanner walks the tree where it is and keeps its sidecar under
  ``data_dir`` — never inside the tree;
* the watcher never attaches an inotify watch — refresh is the auto-sync
  rescan (Drive FSEvents are unreliable; a linked tree can hold tens of
  thousands of directories);
* ``delete_folder`` removes index rows only and never touches disk;
* the sync source cannot be removed on its own — a bare folder left pointing
  at the tree would accept uploads and deletes into it;
* no ghost directories are seeded from disk.

Uploads, mkdir and file/dir deletes are already refused for ANY folder with
a sync source (``routes.folders._require_regular``), so they need no rule here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.models import Folder

IN_PLACE_SOURCE_TYPES: frozenset[str] = frozenset({"google_drive_local", "local_link"})


def is_in_place_type(source_type: str | None) -> bool:
    return source_type in IN_PLACE_SOURCE_TYPES


def indexes_in_place(folder: Folder) -> bool:
    """True when ``folder``'s content lives in a tree Voitta must never write to."""
    return is_in_place_type(folder.source_type)
