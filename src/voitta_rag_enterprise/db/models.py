"""SQLAlchemy models for the SQLite metadata store.

Schema source of truth is ``schema.sql``. These models cover the tables touched
by Stage 1; later stages add ``Chunk``, ``Image``, ``ChunkImageLink``, ``FileAcl``,
``FolderAcl``, etc. Indexes and unique constraints are declared in SQL only;
SQLAlchemy is used for ORM access, not for DDL generation.

Time conventions:
- ``*_at`` columns are Unix epoch seconds (``int(time.time())``).
- ``mtime_ns`` is nanoseconds (matches ``os.stat().st_mtime_ns``).
"""

from __future__ import annotations

import time

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now_s() -> int:
    return int(time.time())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]
    display_name: Mapped[str | None] = mapped_column(default=None)
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[int] = mapped_column(default=_now_s)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str]
    display_name: Mapped[str]
    source_type: Mapped[str] = mapped_column(default="filesystem")
    source_config: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    shared: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[int] = mapped_column(default=_now_s)


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"))
    rel_path: Mapped[str]
    file_cas_id: Mapped[str | None] = mapped_column(default=None)
    size_bytes: Mapped[int | None] = mapped_column(default=None)
    mtime_ns: Mapped[int | None] = mapped_column(default=None)
    added_at: Mapped[int] = mapped_column(default=_now_s)
    last_seen_at: Mapped[int] = mapped_column(default=_now_s)
    last_indexed_at: Mapped[int | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(default="pending")
    pending_embeds: Mapped[int] = mapped_column(default=0)
    embed_round: Mapped[int] = mapped_column(default=0)
    source_url: Mapped[str | None] = mapped_column(default=None)
    tab: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)


class CasRef(Base):
    __tablename__ = "cas_refs"

    cas_id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)  # composite PK with cas_id
    refcount: Mapped[int] = mapped_column(default=0)
    last_decref_at: Mapped[int | None] = mapped_column(default=None)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    chunk_index: Mapped[int]
    chunk_hash: Mapped[str]
    text: Mapped[str]
    char_start: Mapped[int | None] = mapped_column(default=None)
    char_end: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[int] = mapped_column(default=_now_s)


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    image_index: Mapped[int]
    image_cas_id: Mapped[str]
    anchor_chunk: Mapped[int | None] = mapped_column(default=None)
    page: Mapped[int | None] = mapped_column(default=None)
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)
    mime: Mapped[str | None] = mapped_column(default=None)
    # 'figure' (cropped extract, embedded for image search) or
    # 'page_render' (full-page raster, layout context only — no embed).
    kind: Mapped[str] = mapped_column(default="figure")
    created_at: Mapped[int] = mapped_column(default=_now_s)


class ChunkImageLink(Base):
    __tablename__ = "chunk_image_links"

    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), primary_key=True
    )
    distance: Mapped[int]


class FolderAcl(Base):
    __tablename__ = "folder_acl"

    folder_id: Mapped[int] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class FolderUserSettings(Base):
    """Per-user, per-folder MCP-search opt-out. ``active=False`` excludes
    this folder from the user's MCP search queries. Missing row = active.
    """

    __tablename__ = "folder_user_settings"

    folder_id: Mapped[int] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    active: Mapped[bool] = mapped_column(default=True)


class FileAcl(Base):
    __tablename__ = "file_acl"

    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class FolderSyncSource(Base):
    __tablename__ = "folder_sync_sources"

    folder_id: Mapped[int] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True
    )
    source_type: Mapped[str]
    # GitHub
    gh_repo: Mapped[str | None] = mapped_column(default=None)
    gh_path: Mapped[str | None] = mapped_column(default=None)
    gh_branches: Mapped[str | None] = mapped_column(default=None)  # JSON array
    gh_all_branches: Mapped[bool] = mapped_column(default=False)
    gh_extended: Mapped[bool] = mapped_column(default=False)
    gh_auth_method: Mapped[str | None] = mapped_column(default=None)
    gh_username: Mapped[str | None] = mapped_column(default=None)
    gh_pat: Mapped[str | None] = mapped_column(default=None)
    gh_token: Mapped[str | None] = mapped_column(default=None)
    # Google Drive
    gd_client_id: Mapped[str | None] = mapped_column(default=None)
    gd_client_secret: Mapped[str | None] = mapped_column(default=None)
    gd_refresh_token: Mapped[str | None] = mapped_column(default=None)
    gd_service_account_json: Mapped[str | None] = mapped_column(default=None)
    gd_folder_id: Mapped[str | None] = mapped_column(default=None)
    # When True the OAuth redirect URI is built as
    # http://localhost:53682/api/sync/oauth/google/callback — admins can
    # then register only a localhost URL in GCP without exposing the
    # Voitta hostname. A small nginx bridge running on the admin's box
    # proxies the callback back to this server.
    gd_use_loopback: Mapped[bool] = mapped_column(default=False)
    # When True, sync downloads only ordinary binary files (PDF, DOCX,
    # images, …) and skips Google-native Docs/Sheets/Slides/Forms. Lets
    # sync run when the project hasn't enabled the Docs/Sheets/Slides/Forms
    # APIs (only the Drive API is then required). Default 0 = export
    # native files too (the all-or-nothing preflight applies).
    gd_files_only: Mapped[bool] = mapped_column(default=False)
    # Microsoft (shared by ``sharepoint`` and ``teams`` source types).
    # ``ms_auth_method`` is one of: "oauth" (delegated, refresh-token),
    # "app_secret" (client-credentials with a client_secret), "app_cert"
    # (client-credentials with a PEM private key). For oauth we also
    # store ms_refresh_token; for app_* the tenant-wide app credentials
    # are enough to mint tokens on demand.
    ms_tenant_id: Mapped[str | None] = mapped_column(default=None)
    ms_client_id: Mapped[str | None] = mapped_column(default=None)
    ms_client_secret: Mapped[str | None] = mapped_column(default=None)
    ms_cert_pem: Mapped[str | None] = mapped_column(default=None)
    ms_auth_method: Mapped[str | None] = mapped_column(default=None)
    ms_refresh_token: Mapped[str | None] = mapped_column(default=None)
    # Same loopback story as gdrive: when True the OAuth redirect is
    # http://localhost:53682/api/sync/oauth/microsoft/callback so admins
    # only register a localhost URL in Azure AD.
    ms_use_loopback: Mapped[bool] = mapped_column(default=False)
    # SharePoint-specific. ``sp_selected_sites`` is a JSON array of
    # ``{"id": str, "displayName": str, "webUrl": str}``; ignored when
    # ``sp_all_sites`` is True (then every accessible site is synced).
    sp_selected_sites: Mapped[str | None] = mapped_column(default=None)
    sp_all_sites: Mapped[bool] = mapped_column(default=False)
    # Teams-specific. ``tm_user_mode`` is "me" (delegated only) or
    # "specific" (one mailbox) or "all_users" (every user in tenant —
    # app-only). ``tm_user_id`` is required for "specific".
    # ``tm_include_attended`` adds /communications/callRecords lookup
    # so meetings the user only attended (not organized) are captured.
    tm_user_mode: Mapped[str | None] = mapped_column(default=None)
    tm_user_id: Mapped[str | None] = mapped_column(default=None)
    tm_include_attended: Mapped[bool] = mapped_column(default=True)
    # NFS — admin sets the root via settings, user picks one or more
    # subpaths under it via the sync UI's tree picker.
    # ``nfs_subpaths`` is a JSON array of POSIX paths *relative* to the
    # root (never absolute, never with ``..``). The legacy
    # ``nfs_subpath`` column held a single value; it's auto-migrated
    # into a one-element array on first read and kept as a fallback
    # source-of-truth for one release before removal.
    nfs_subpath: Mapped[str | None] = mapped_column(default=None)
    nfs_subpaths: Mapped[str | None] = mapped_column(default=None)
    # Status
    sync_status: Mapped[str] = mapped_column(default="idle")
    sync_error: Mapped[str | None] = mapped_column(default=None)
    last_synced_at: Mapped[int | None] = mapped_column(default=None)
    # Periodic auto-sync (driven by services.scheduler).
    auto_sync_enabled: Mapped[bool] = mapped_column(default=False)
    auto_sync_hours: Mapped[int] = mapped_column(default=6)
    created_at: Mapped[int] = mapped_column(default=_now_s)
    updated_at: Mapped[int] = mapped_column(default=_now_s, onupdate=_now_s)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str]
    prefix: Mapped[str]
    key_hash: Mapped[str]
    created_at: Mapped[int] = mapped_column(default=_now_s)
    last_used_at: Mapped[int | None] = mapped_column(default=None)


class AuthProvider(Base):
    """Admin-managed list of OAuth provider credentials.

    Each row is one ``(provider, client_id, client_secret)`` triple plus
    a label and an enabled flag. Two rows for the same provider are
    intentionally allowed (e.g. two Google clients) — the only uniqueness
    is the primary key. Login flow currently consumes only Google rows
    where ``enabled=True``; Microsoft / GitHub are accepted as values but
    not yet wired anywhere.

    Bootstrap: on every startup the lifespan upserts a row for the
    ``VOITTA_GOOGLE_AUTH_CLIENT_ID``/``_SECRET`` pair so an .env-managed
    deployment always has at least one entry. Deleting that row in the
    UI only sticks until the next restart, by design — to truly remove
    it, drop the env vars too.
    """

    __tablename__ = "auth_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str]  # "google" | "microsoft" | "github" | …
    label: Mapped[str] = mapped_column(default="")
    client_id: Mapped[str]
    client_secret: Mapped[str] = mapped_column(default="")
    # Required for Microsoft (Azure AD tenant id or `*.onmicrosoft.com`
    # domain); ignored for Google/GitHub. Stored on the same row so the
    # sync UI's provider picker can prefill all three fields in one click.
    tenant_id: Mapped[str] = mapped_column(default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    # Marker for rows seeded by the .env bootstrap. Used only to log when a
    # missing seed-row gets re-created on the next restart; not exposed
    # to the API today.
    source: Mapped[str] = mapped_column(default="user")  # "user" | "env"
    created_at: Mapped[int] = mapped_column(default=_now_s)
    updated_at: Mapped[int] = mapped_column(default=_now_s)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    payload: Mapped[str]
    state: Mapped[str] = mapped_column(default="queued")
    priority: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    dedup_key: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    enqueued_at: Mapped[int] = mapped_column(default=_now_s)
    started_at: Mapped[int | None] = mapped_column(default=None)
    finished_at: Mapped[int | None] = mapped_column(default=None)
    # JSON summary returned by the handler on success (sync stats, etc.);
    # NULL for handlers that report nothing. Shown in the Jobs panel detail.
    result: Mapped[str | None] = mapped_column(default=None)


class Group(Base):
    """Organizational user group. Membership only — no folder-ACL effect yet."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[int] = mapped_column(default=_now_s)


class UserGroup(Base):
    """user ↔ group membership (composite PK, cascade-delete both ways)."""

    __tablename__ = "user_groups"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
