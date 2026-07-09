"""Folder sync configuration + trigger endpoints.

Package layout — one module per sync source, dispatched through a
handler registry so the core endpoints stay source-agnostic:

- ``base``          shared routers, access checks, publish helpers
- ``registry``      per-source-type handler table (apply/build_out/clear/…)
- ``core``          envelope schemas + GET/PUT/DELETE//error//trigger
- ``github``        repo sync + branch picker
- ``google_drive``  Drive API sync + OAuth init/callback + folder pickers
- ``google_local``  Drive for Desktop mount (macOS, no credentials)
- ``microsoft``     SharePoint + Teams (shared ms_* auth) + OAuth + pickers
- ``jira``          issue sync + project picker
- ``confluence``    page sync + space picker
- ``nfs``           admin-rooted mount + directory picker

Per-folder routes live under ``/folders/{folder_id}/sync`` (``router``);
the OAuth callbacks and other folder-agnostic endpoints live under
``/sync`` (``oauth_router``) because provider callback URLs are registered
once and cannot be parameterised.

Importing ``core`` pulls in every source module, which registers its
handler and attaches its routes as a side effect.
"""

from .base import oauth_router, router
from .core import SyncSourceIn, SyncSourceOut, _to_out, to_out

__all__ = [
    "SyncSourceIn",
    "SyncSourceOut",
    "_to_out",
    "oauth_router",
    "router",
    "to_out",
]
