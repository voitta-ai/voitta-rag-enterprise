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
    created_at: Mapped[int] = mapped_column(default=_now_s)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str]
    display_name: Mapped[str]
    source_type: Mapped[str] = mapped_column(default="filesystem")
    source_config: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    managed: Mapped[bool] = mapped_column(default=False)
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
    # Status
    sync_status: Mapped[str] = mapped_column(default="idle")
    sync_error: Mapped[str | None] = mapped_column(default=None)
    last_synced_at: Mapped[int | None] = mapped_column(default=None)
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
